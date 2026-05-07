import logging
from datetime import UTC, datetime

from quantamental.config.settings import (
    REGIME_LABELS,
    REGIME_MODERATE_OFF_MIN,
    REGIME_MODERATE_ON_MIN,
    REGIME_NEUTRAL_MIN,
    REGIME_RISK_ON_MIN,
)

logger = logging.getLogger(__name__)


def classify_regime(composite_score: int) -> str:
    """Map composite score (-8 to +8) to a regime label."""
    if composite_score >= REGIME_RISK_ON_MIN:
        return REGIME_LABELS["RISK_ON"]
    if composite_score >= REGIME_MODERATE_ON_MIN:
        return REGIME_LABELS["MODERATE_ON"]
    if composite_score >= REGIME_NEUTRAL_MIN:
        return REGIME_LABELS["NEUTRAL"]
    if composite_score >= REGIME_MODERATE_OFF_MIN:
        return REGIME_LABELS["MODERATE_OFF"]
    return REGIME_LABELS["RISK_OFF"]


def compute_confirmed_regime(
    today_regime: str,
    yesterday_regime: str | None,
    yesterday_confirmed: str | None,
) -> str:
    """Apply the 2-day regime confirmation rule (spec §10, fix D5).

    The "confirmed" regime only changes when today's regime matches yesterday's
    raw regime. Single-day flips do NOT change the confirmed regime — preventing
    spurious trading triggers from one-day signal noise.

    Logic:
        - First-ever observation: confirmed = today's regime (no history)
        - Today == yesterday raw: regime flip is now 2 consecutive days → confirm
        - Today != yesterday raw: carry forward yesterday's confirmed regime

    Args:
        today_regime: regime computed from today's composite score
        yesterday_regime: regime stored yesterday (None on first run)
        yesterday_confirmed: confirmed_regime stored yesterday (None on first run)

    Returns:
        The confirmed regime to write today.
    """
    # First observation — no history to confirm against
    if yesterday_regime is None:
        return today_regime

    # 2 consecutive days agree → today's regime is confirmed
    if today_regime == yesterday_regime:
        return today_regime

    # Today disagrees with yesterday — keep the most recent confirmed regime
    # (or today's if there's never been a confirmed one)
    return yesterday_confirmed if yesterday_confirmed is not None else today_regime


def run_and_store(
    yield_df,
    vix_df,
    fed_df,
    credit_df,
    writer=None,
) -> dict:
    """Compute signals, classify regime, optionally persist to QuestDB.

    writer: questdb_writer module (injected to avoid circular imports).
    Returns the full signal row dict including confirmed_regime per D5.
    """
    from quantamental.signals.macro import compute_all_signals

    signals = compute_all_signals(yield_df, vix_df, fed_df, credit_df)
    today_regime = classify_regime(signals["composite_score"])

    # D5: derive confirmed_regime by reading yesterday's pair from DB
    yesterday_regime, yesterday_confirmed = (None, None)
    if writer is not None:
        try:
            yesterday_regime, yesterday_confirmed = writer.latest_regime_pair()
        except Exception as exc:
            logger.warning("Could not read yesterday's regime (%s) — defaulting confirmed = today", exc)

    confirmed_regime = compute_confirmed_regime(
        today_regime=today_regime,
        yesterday_regime=yesterday_regime,
        yesterday_confirmed=yesterday_confirmed,
    )

    row = {
        "ts": datetime.now(UTC),
        **signals,
        "regime": today_regime,
        "confirmed_regime": confirmed_regime,
    }

    if today_regime != confirmed_regime:
        logger.info(
            "Regime: %s (raw) | %s (confirmed — pending 2nd consecutive day)",
            today_regime, confirmed_regime,
        )
    else:
        logger.info("Regime: %s (confirmed)", today_regime)

    if writer is not None:
        writer.write_signals(row)

    return row


# ── Month 2: 3-layer composite (macro + sector + stock) ───────────────────────
#
# Per spec §5.1-5.5: combine the three signal layers into a single normalized
# score and map to a portfolio action.
#
# Layer weights are read from signals_registry.yaml so the user can rebalance
# macro vs sector vs stock influence without touching Python code.
# Defaults (if registry is unavailable): macro=1.0, sector=0.8, stock=0.6.

def _layer_weights() -> tuple[float, float, float]:
    """Return (macro, sector, stock) layer weights from the registry."""
    from quantamental.signals import registry as _reg
    return (
        _reg.layer_weight("macro"),
        _reg.layer_weight("sector"),
        _reg.layer_weight("stock"),
    )


# Fallback constants — used only when registry is unavailable.
# Also kept so existing imports of WEIGHT_MACRO / WEIGHT_SECTOR / WEIGHT_STOCK
# from other modules don't break.
WEIGHT_MACRO = 1.0
WEIGHT_SECTOR = 0.8
WEIGHT_STOCK = 0.6

# Maximum possible weighted score with default weights, used to normalize to [-9, +9]
# = 8*1.0 + 8*0.8 + 7*0.6 = 18.6  (macro/sector max=8, stock max=7)
MAX_WEIGHTED = 8 * WEIGHT_MACRO + 8 * WEIGHT_SECTOR + 7 * WEIGHT_STOCK


def adjusted_rsi_score(rsi_score: int, ema_score: int) -> int:
    """Trend-adjusted RSI per spec §5.4.

    In a strong uptrend, an "overbought" RSI is reflecting momentum, not
    exhaustion. In a strong downtrend, an "oversold" RSI may be a falling
    knife. This function softens those misleading signals to neutral.
    """
    # Strong uptrend: an overbought reading (-1) is just trend strength
    if ema_score == 2 and rsi_score == -1:
        return 0
    # Strong downtrend: an oversold reading (+1) may be catching a falling knife
    if ema_score == -2 and rsi_score == 1:
        return 0
    return rsi_score


def normalize_composite(macro: int, sector: int, stock: float) -> float:
    """Compute the weighted composite and normalize to [-9, +9] per spec §5.3.

    Layer weights are read from signals_registry.yaml at call time so changes
    take effect on the next pipeline run without restarting.

    Args:
        macro: macro composite, range [-8, +8]
        sector: sector composite, range [-8, +8]
        stock: average stock composite (per-stock or universe avg), range [-7, +7]
            float because it's typically averaged across many tickers.

    Returns:
        Float in [-9, +9], rounded to 1 decimal place.
    """
    w_macro, w_sector, w_stock = _layer_weights()
    max_weighted = 8 * w_macro + 8 * w_sector + 7 * w_stock
    weighted = macro * w_macro + sector * w_sector + stock * w_stock
    normalized = (weighted / max_weighted) * 9 if max_weighted else 0.0
    return round(max(-9.0, min(9.0, normalized)), 1)


# Action mapping per spec §5.5
def map_action(normalized_score: float) -> tuple[str, str]:
    """Map a normalized composite score to a (regime, action) pair."""
    if normalized_score >= 7:
        return ("STRONG_BUY",  "Accelerate batch entries; deploy reserve cash")
    if normalized_score >= 4:
        return ("BUY",         "Proceed with scheduled entries")
    if normalized_score >= 1:
        return ("MILD_BUY",    "Maintain positions; selective new entries")
    if normalized_score >= -1:
        return ("NEUTRAL",     "Hold; no new entries")
    if normalized_score >= -4:
        return ("MILD_SELL",   "Pause entries; tighten stops 5%")
    if normalized_score >= -7:
        return ("SELL",        "Reduce exposure 25-50%")
    return ("STRONG_SELL",      "Emergency de-risk; activate hedges")


def macro_override_blocks_buys(macro_score: int) -> bool:
    """Spec §5.6.1: macro RISK_OFF blocks new long positions regardless
    of sector/stock signals. Returns True if buys should be blocked."""
    return macro_score < -4


def run_composite(
    macro_score: int,
    sector_score: int,
    avg_stock_score: float,
    persist: bool = True,
) -> dict:
    """Run the 3-layer aggregator and optionally persist to QuestDB.

    Returns: dict with all intermediate values and final action.
    """
    w_macro, w_sector, w_stock = _layer_weights()
    weighted = (macro_score * w_macro
                + sector_score * w_sector
                + avg_stock_score * w_stock)
    normalized = normalize_composite(macro_score, sector_score, avg_stock_score)
    regime, action = map_action(normalized)

    # Macro override: if macro is RISK_OFF, downgrade any "buy" action
    if macro_override_blocks_buys(macro_score) and "BUY" in regime:
        logger.warning(
            "Macro override: macro_score=%d < -4 → blocking %s (forced to NEUTRAL)",
            macro_score, regime,
        )
        regime = "NEUTRAL"
        action = "Hold; no new entries (macro RISK_OFF override)"

    row = {
        "ts": datetime.now(UTC),
        "macro_score":        macro_score,
        "sector_score":       sector_score,
        "avg_stock_score":    avg_stock_score,
        "weighted_composite": weighted,
        "normalized_score":   normalized,
        "regime":             regime,
        "action":             action,
    }

    logger.info(
        "Composite: macro=%+d sector=%+d stock=%+.1f → weighted=%+.2f normalized=%+.1f → %s",
        macro_score, sector_score, avg_stock_score, weighted, normalized, regime,
    )

    if persist:
        from quantamental.data.ingest.questdb_writer import write_composite_signal
        write_composite_signal(row)

    return row
