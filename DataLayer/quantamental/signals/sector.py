"""
Sector timing signal (Month 2 §3).

Signal A: SOX/SPX Relative Strength
    Ratio of SMH (semiconductor ETF) over SPY (broad market). When semiconductors
    outperform the market, it signals sustained AI infrastructure demand.

This is the core sector signal — generic, works for any equity-momentum thesis,
not just AI infra. AI-specific signals (B/C/D — TSMC revenue, Capex Surprise,
API pricing) live in `signals/sector_ai_infra.py`.

Implementation per Month 2 spec §3.1:
    fast EMA = 20 days, slow EMA = 60 days
    +2 if fast > slow AND ratio at 20-day high
    +1 if fast > slow
     0 if fast within 1% of slow
    -1 if fast < slow
    -2 if fast < slow AND ratio at 20-day low
"""

import logging
from datetime import UTC, datetime
from typing import TypedDict

import pandas as pd

logger = logging.getLogger(__name__)

# Default tickers used as proxies for SOX (semis) and SPX (broad market).
# Both must exist in daily_ohlcv and have at least 60 days of history.
DEFAULT_SEMI_PROXY = "SMH"
DEFAULT_BROAD_PROXY = "SPY"

EMA_FAST = 20
EMA_SLOW = 60
NEUTRAL_BAND_PCT = 0.01      # 1% — within this is neutral
HIGH_LOW_TOLERANCE = 0.99    # within 1% of 20-day extreme counts as "at high/low"


class SoxSpxResult(TypedDict):
    ratio: float
    ema_fast: float
    ema_slow: float
    signal: int


def calc_sox_spx_signal(smh_close: pd.Series, spy_close: pd.Series) -> SoxSpxResult:
    """Compute Signal A (SOX/SPX relative strength).

    Args:
        smh_close: SMH adjusted close prices, indexed by date, ascending
        spy_close: SPY adjusted close prices, same index as smh_close

    Returns:
        Dict with ratio, ema_fast, ema_slow, signal (-2..+2).
        Returns neutral (signal=0) if insufficient history.
    """
    if smh_close.empty or spy_close.empty:
        logger.warning("calc_sox_spx_signal: empty input series")
        return {"ratio": 0.0, "ema_fast": 0.0, "ema_slow": 0.0, "signal": 0}

    # Align indices and compute ratio
    aligned = pd.concat([smh_close, spy_close], axis=1, join="inner").dropna()
    if len(aligned) < EMA_SLOW:
        logger.warning("calc_sox_spx_signal: only %d aligned rows, need %d",
                       len(aligned), EMA_SLOW)
        return {"ratio": 0.0, "ema_fast": 0.0, "ema_slow": 0.0, "signal": 0}

    smh = aligned.iloc[:, 0]
    spy = aligned.iloc[:, 1]
    ratio = smh / spy

    ema_fast = ratio.ewm(span=EMA_FAST, adjust=False).mean()
    ema_slow = ratio.ewm(span=EMA_SLOW, adjust=False).mean()

    latest_ratio = float(ratio.iloc[-1])
    latest_fast = float(ema_fast.iloc[-1])
    latest_slow = float(ema_slow.iloc[-1])

    # 20-day extremes for "at high" / "at low" detection
    window_20 = ratio.iloc[-EMA_FAST:]
    high_20d = float(window_20.max())
    low_20d = float(window_20.min())

    # Neutral band check (fast and slow within 1% of each other)
    if latest_slow > 0 and abs(latest_fast - latest_slow) / latest_slow < NEUTRAL_BAND_PCT:
        signal = 0
    elif latest_fast > latest_slow:
        # Bullish — strength of signal depends on whether we're at the 20-day high
        signal = 2 if latest_ratio >= high_20d * HIGH_LOW_TOLERANCE else 1
    else:
        # Bearish — strength depends on whether we're at the 20-day low
        # `1.01` because we want "within 1% of low" symmetrically
        signal = -2 if latest_ratio <= low_20d * (2 - HIGH_LOW_TOLERANCE) else -1

    return {
        "ratio":    latest_ratio,
        "ema_fast": latest_fast,
        "ema_slow": latest_slow,
        "signal":   signal,
    }


def compute_sox_spx_from_db(
    semi_proxy: str = DEFAULT_SEMI_PROXY,
    broad_proxy: str = DEFAULT_BROAD_PROXY,
    lookback_days: int = 90,
) -> SoxSpxResult:
    """Pull SMH/SPY from QuestDB and compute the signal.

    Reads `lookback_days` of recent OHLCV data — enough for the 60-day EMA.
    """
    from data.ingest.questdb_writer import query

    sql = f"""
        SELECT symbol, ts, close
        FROM daily_ohlcv
        WHERE symbol IN ('{semi_proxy}', '{broad_proxy}')
          AND ts > dateadd('d', -{lookback_days * 2}, now())
        ORDER BY ts
    """
    df = query(sql)
    if df.empty:
        logger.warning("compute_sox_spx_from_db: no data found")
        return {"ratio": 0.0, "ema_fast": 0.0, "ema_slow": 0.0, "signal": 0}

    # Pivot: one column per symbol, indexed by ts
    wide = df.pivot(index="ts", columns="symbol", values="close").sort_index()

    if semi_proxy not in wide.columns or broad_proxy not in wide.columns:
        missing = [s for s in (semi_proxy, broad_proxy) if s not in wide.columns]
        logger.warning("compute_sox_spx_from_db: missing tickers %s", missing)
        return {"ratio": 0.0, "ema_fast": 0.0, "ema_slow": 0.0, "signal": 0}

    return calc_sox_spx_signal(wide[semi_proxy], wide[broad_proxy])


# ── Sector composite ──────────────────────────────────────────────────────────

def compute_sector_composite(
    sox_spx_signal: int,
    tsmc_signal: int = 0,
    capex_signal: int = 0,
    api_pricing_signal: int = 0,
) -> int:
    """Registry-weighted sector composite normalised to [-8, +8].

    Per spec §3.5, signals B/C/D (manual/AI-infra-specific) default to 0 if not
    yet entered — so the composite degrades gracefully to just Signal A in the
    early days of operation.

    Disabled signals contribute 0 to the weighted sum but are excluded from
    max_possible so the composite range is preserved regardless of how many
    signals are active.
    """
    from signals import registry as _reg

    _COMPONENTS = {
        "sox_spx":        sox_spx_signal,
        "tsmc_revenue":   tsmc_signal,
        "capex_surprise": capex_signal,
        "api_pricing":    api_pricing_signal,
    }

    weighted_sum = 0.0
    max_possible = 0.0
    for name, score in _COMPONENTS.items():
        if _reg.is_enabled("sector", name):
            w = _reg.signal_weight("sector", name)
            weighted_sum += score * w
            max_possible += 2.0 * w

    composite = round(weighted_sum / max_possible * 8) if max_possible else 0
    return max(-8, min(8, composite))


def run_sector_signals(persist: bool = True) -> dict:
    """Compute all sector signals and optionally write to QuestDB.

    Returns a dict with all individual signal scores, intermediate values,
    and the composite.

    Persistence is OFF by default for unit-testing convenience; the daily
    pipeline calls with persist=True.
    """
    from signals.sector_ai_infra import (
        latest_tsmc_signal,
        latest_capex_signal,
        latest_api_pricing_signal,
    )

    sox = compute_sox_spx_from_db()
    tsmc = latest_tsmc_signal()
    capex = latest_capex_signal()
    api_p = latest_api_pricing_signal()

    composite = compute_sector_composite(
        sox["signal"], tsmc, capex, api_p,
    )

    row = {
        "ts": datetime.now(UTC),
        "sox_spx_ratio":      sox["ratio"],
        "sox_spx_ema20":      sox["ema_fast"],
        "sox_spx_ema60":      sox["ema_slow"],
        "sox_spx_signal":     sox["signal"],
        "tsmc_signal":        tsmc,
        "capex_signal":       capex,
        "api_pricing_signal": api_p,
        "sector_composite":   composite,
    }

    logger.info(
        "Sector signals: A(SOX/SPX)=%+d B(TSMC)=%+d C(Capex)=%+d D(API)=%+d → composite=%+d",
        sox["signal"], tsmc, capex, api_p, composite,
    )

    if persist:
        from data.ingest.questdb_writer import write_sector_signals
        write_sector_signals(row)

    return row
