"""
Stock-level signals (Month 2 §4).

Adapted parameters for the limited 2024-06+ history:
    EMA: 20/60 (spec says 50/200 — too long for our data)
    RSI: 14 (unchanged)
    Volume MA: 20 (unchanged)
    PEAD: 4-week duration (spec range 4-6 weeks)

Each ticker scored independently. Daily output: one row per ticker per day.

Signals:
    1. Dual EMA system (20/60) — trend
    2. RSI(14) — overbought/oversold
    3. Volume confirmation — breakout validation
    4. PEAD — post-earnings drift
"""

import logging
from datetime import UTC, date, datetime
from typing import TypedDict

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Spec-adjusted EMA windows (60-day warmup leaves a usable backtest window)
EMA_FAST = 20
EMA_SLOW = 60

# RSI parameters (spec §4.2)
RSI_PERIOD = 14
RSI_DEEP_OVERSOLD = 25
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
RSI_DEEP_OVERBOUGHT = 75

# Volume parameters (spec §4.3)
VOLUME_MA_PERIOD = 20
VOLUME_HIGH_RATIO = 1.5
VOLUME_BREAKOUT_RETURN_THRESHOLD = 0.02  # 2% daily move

# PEAD parameters (spec §4.4 — adapted to 4-week window)
PEAD_DURATION_DAYS = 28


# ── Per-ticker scorers ────────────────────────────────────────────────────────

class EmaResult(TypedDict):
    ema_fast: float
    ema_slow: float
    score: int
    event: str | None  # GOLDEN_CROSS | DEATH_CROSS | None


def score_ema(close: pd.Series) -> EmaResult:
    """Dual EMA(20/60) trend system + golden/death cross detection."""
    if len(close) < EMA_SLOW + 1:
        return {"ema_fast": 0.0, "ema_slow": 0.0, "score": 0, "event": None}

    ema_fast = close.ewm(span=EMA_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=EMA_SLOW, adjust=False).mean()

    price = float(close.iloc[-1])
    fast = float(ema_fast.iloc[-1])
    slow = float(ema_slow.iloc[-1])

    # Trend score per spec §4.1
    # Original spec uses 50/200 EMA; we adapt to 20/60 but the structure is the same.
    # "Pullback in uptrend" = uptrend regime (fast > slow) where price dipped below fast EMA.
    # "Recovery attempt"    = downtrend regime (fast < slow) where price rose above fast EMA.
    if fast > slow and price > fast:
        score = 2   # strong uptrend, price above both EMAs
    elif fast > slow and price > slow:
        score = 1   # pullback in uptrend (price between EMAs)
    elif fast < slow and price < fast:
        score = -2  # strong downtrend, price below both EMAs
    elif fast < slow and price < slow:
        score = -1  # recovery attempt (price between EMAs in downtrend regime)
    else:
        score = 0   # oscillating around both EMAs

    # Cross detection — compare today vs yesterday
    prev_fast = float(ema_fast.iloc[-2])
    prev_slow = float(ema_slow.iloc[-2])
    event: str | None = None
    if prev_fast <= prev_slow and fast > slow:
        event = "GOLDEN_CROSS"
    elif prev_fast >= prev_slow and fast < slow:
        event = "DEATH_CROSS"

    return {"ema_fast": fast, "ema_slow": slow, "score": score, "event": event}


def calc_rsi(close: pd.Series, period: int = RSI_PERIOD) -> float:
    """Wilder's RSI(period). Returns the latest value, or 50 if insufficient data."""
    if len(close) < period + 1:
        return 50.0

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # Wilder smoothing — equivalent to EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    last = rsi.iloc[-1]
    if not pd.isna(last):
        return float(last)

    # Edge cases when avg_loss == 0 (all gains) or avg_gain == 0 (all losses)
    last_gain = float(avg_gain.iloc[-1] or 0)
    last_loss = float(avg_loss.iloc[-1] or 0)
    if last_loss == 0 and last_gain > 0:
        return 100.0
    if last_gain == 0 and last_loss > 0:
        return 0.0
    return 50.0


def score_rsi(rsi_value: float) -> int:
    """Score RSI per spec §4.2 buckets."""
    if rsi_value < RSI_DEEP_OVERSOLD:    return 2
    if rsi_value < RSI_OVERSOLD:         return 1
    if rsi_value <= RSI_OVERBOUGHT:      return 0
    if rsi_value <= RSI_DEEP_OVERBOUGHT: return -1
    return -2


def score_volume(close: pd.Series, volume: pd.Series) -> tuple[float, int]:
    """Volume confirmation per spec §4.3.

    Returns (vol_ratio, signal) where signal is -1, 0, or +1.
    """
    if len(close) < VOLUME_MA_PERIOD + 1 or len(volume) < VOLUME_MA_PERIOD + 1:
        return 0.0, 0

    avg_vol = volume.rolling(VOLUME_MA_PERIOD).mean()
    if pd.isna(avg_vol.iloc[-1]) or avg_vol.iloc[-1] == 0:
        return 0.0, 0

    vol_ratio = float(volume.iloc[-1] / avg_vol.iloc[-1])
    daily_return = float(close.pct_change().iloc[-1] or 0)

    if vol_ratio > VOLUME_HIGH_RATIO and daily_return > VOLUME_BREAKOUT_RETURN_THRESHOLD:
        signal = 1   # bullish breakout confirmed
    elif vol_ratio > VOLUME_HIGH_RATIO and daily_return < -VOLUME_BREAKOUT_RETURN_THRESHOLD:
        signal = -1  # bearish breakdown confirmed
    else:
        signal = 0

    return vol_ratio, signal


def score_pead(
    eps_surprise_pct: float | None,
    days_since_earnings: int,
    duration_days: int = PEAD_DURATION_DAYS,
) -> int:
    """Post-Earnings Announcement Drift signal with linear decay (spec §4.4).

    Args:
        eps_surprise_pct: (actual - consensus) / |consensus|, e.g. 0.08 = 8% beat
        days_since_earnings: days elapsed since the report date (0 = today)
        duration_days: how long the signal stays active (default 4 weeks)

    Returns: int in [-2, +2] with linear decay.
    """
    if eps_surprise_pct is None or pd.isna(eps_surprise_pct) or days_since_earnings >= duration_days:
        return 0
    if days_since_earnings < 0:
        return 0   # earnings in the future, no signal yet

    # Raw score per spec §4.4 (we treat "guidance raised" as additional caller logic)
    if eps_surprise_pct > 0.10:    raw = 2
    elif eps_surprise_pct > 0.05:  raw = 1
    elif eps_surprise_pct > -0.05: raw = 0
    elif eps_surprise_pct > -0.10: raw = -1
    else:                          raw = -2

    decay = 1.0 - (days_since_earnings / duration_days)
    return int(round(raw * decay))


# ── Composite + bulk computation ──────────────────────────────────────────────

def stock_composite_score(ema_score: int, rsi_score: int,
                          volume_signal: int, pead_score: int) -> int:
    """Registry-weighted stock composite normalised to [-7, +7].

    Per-signal max absolute values (used for normalisation):
        ema: ±2, rsi: ±2, volume: ±1, pead: ±2  →  total max = 7
    """
    from quantamental.signals import registry as _reg

    # (registry_name, score, max_abs_value_for_this_signal)
    _COMPONENTS = [
        ("ema",    ema_score,     2),
        ("rsi",    rsi_score,     2),
        ("volume", volume_signal, 1),
        ("pead",   pead_score,    2),
    ]

    weighted_sum = 0.0
    max_possible = 0.0
    for name, score, max_val in _COMPONENTS:
        if _reg.is_enabled("stock", name):
            w = _reg.signal_weight("stock", name)
            weighted_sum += score * w
            max_possible += max_val * w

    composite = round(weighted_sum / max_possible * 7) if max_possible else 0
    return max(-7, min(7, composite))


def score_one_ticker(
    df: pd.DataFrame,
    earnings_event: dict | None = None,
    asof: date | None = None,
) -> dict:
    """Compute all stock signals for one ticker on the most recent day.

    Args:
        df: per-ticker OHLCV with columns [ts, close, volume], sorted ascending
        earnings_event: optional dict with keys {report_date, eps_surprise_pct}
        asof: anchor date for PEAD decay (defaults to today)

    Returns dict with all signal values + composite, ready to write to stock_signals.
    """
    if df.empty:
        return _neutral_row()

    asof = asof or date.today()

    close = df["close"].reset_index(drop=True)
    volume = df["volume"].reset_index(drop=True)

    ema = score_ema(close)
    rsi_value = calc_rsi(close)
    rsi_signal = score_rsi(rsi_value)
    vol_ratio, vol_signal = score_volume(close, volume)

    pead_signal = 0
    if earnings_event and earnings_event.get("report_date"):
        days_since = (asof - earnings_event["report_date"]).days
        pead_signal = score_pead(earnings_event.get("eps_surprise_pct"), days_since)

    composite = stock_composite_score(ema["score"], rsi_signal, vol_signal, pead_signal)

    return {
        "close":           float(close.iloc[-1]),
        "ema_20":          ema["ema_fast"],
        "ema_60":          ema["ema_slow"],
        "ema_signal":      ema["score"],
        "ema_event":       ema["event"],          # not persisted in stock_signals
        "rsi_14":          rsi_value,
        "rsi_signal":      rsi_signal,
        "volume_ratio":    vol_ratio,
        "volume_signal":   vol_signal,
        "pead_signal":     pead_signal,
        "stock_composite": composite,
    }


def _neutral_row() -> dict:
    return {
        "close": 0.0, "ema_20": 0.0, "ema_60": 0.0, "ema_signal": 0,
        "ema_event": None, "rsi_14": 50.0, "rsi_signal": 0,
        "volume_ratio": 0.0, "volume_signal": 0, "pead_signal": 0,
        "stock_composite": 0,
    }


def compute_stock_signals_for_universe(
    universe: list[str] | None = None,
    asof: date | None = None,
    persist: bool = True,
) -> pd.DataFrame:
    """Compute stock signals for every ticker in the universe (one row each).

    Reads OHLCV from QuestDB (last 90 days per ticker — enough for EMA60).
    Writes to stock_signals table by default. Also writes golden/death cross
    events to signal_events.

    Args:
        universe: list of tickers (defaults to research universe)
        asof: anchor date for PEAD decay (defaults to today)
        persist: write results to QuestDB

    Returns: DataFrame of all per-ticker signal rows.
    """
    from quantamental.config.universe import load_research_universe
    from quantamental.data.ingest.questdb_writer import (
        query,
        symbol_list_clause,
        write_signal_event,
        write_stock_signals,
    )

    asof = asof or date.today()
    universe = universe or load_research_universe()

    # Bulk-load OHLCV history for all tickers in one query
    if not universe:
        return pd.DataFrame()

    clause, params = symbol_list_clause(universe)
    sql = f"""
        SELECT symbol, ts, close, volume
        FROM daily_ohlcv
        WHERE symbol IN ({clause})
          AND ts > dateadd('d', -120, now())
        ORDER BY symbol, ts
    """
    all_data = query(sql, params)
    if all_data.empty:
        logger.warning("compute_stock_signals_for_universe: no OHLCV data")
        return pd.DataFrame()

    # Load any active earnings events (for PEAD)
    earnings_map = _load_active_earnings(asof)

    rows = []
    events = []
    ts_now = datetime.now(UTC)

    for symbol, group in all_data.groupby("symbol"):
        result = score_one_ticker(
            group.sort_values("ts"),
            earnings_event=earnings_map.get(symbol),
            asof=asof,
        )
        result["symbol"] = symbol
        result["ts"] = ts_now
        rows.append(result)

        if result.get("ema_event"):
            events.append((symbol, result["ema_event"], result["ema_signal"]))

    df = pd.DataFrame(rows)

    if persist:
        # Drop the ema_event column before writing (not in schema)
        write_df = df.drop(columns=["ema_event"], errors="ignore")
        write_stock_signals(write_df)

        # Persist golden/death cross events
        for symbol, evt, impact in events:
            write_signal_event(symbol, evt, signal_impact=impact, ts=ts_now)

    n_golden = sum(1 for _, evt, _ in events if evt == "GOLDEN_CROSS")
    n_death = sum(1 for _, evt, _ in events if evt == "DEATH_CROSS")
    logger.info(
        "Stock signals: %d tickers scored, golden_crosses=%d death_crosses=%d",
        len(df), n_golden, n_death,
    )
    return df


def _load_active_earnings(asof: date) -> dict[str, dict]:
    """Read earnings events from SQLite for any ticker still inside the PEAD window."""
    from quantamental.signals.earnings import load_earnings_events

    cutoff = asof - pd.Timedelta(days=PEAD_DURATION_DAYS).to_pytimedelta()
    events = load_earnings_events(start=cutoff, end=asof)
    if events.empty:
        return {}

    result = {}
    for _, r in events.iterrows():
        try:
            d = date.fromisoformat(str(r["report_date"]))
        except Exception:
            continue
        # Keep the most recent event per ticker
        symbol = str(r["symbol"]).upper()
        prev = result.get(symbol)
        if prev is None or d > prev["report_date"]:
            result[symbol] = {
                "report_date":      d,
                "eps_surprise_pct": (r["surprise_pct"] / 100.0)
                                    if r["surprise_pct"] is not None and not pd.isna(r["surprise_pct"])
                                    else None,
            }
    return result
