"""
Macro signal scoring functions.

Each scorer returns an int in [-2, +2].
Composite score is registry-weighted and normalised to [-8, +8].
"""

import logging

import numpy as np
import pandas as pd

from config.settings import (
    CREDIT_FAST_MA,
    CREDIT_SLOW_MA,
    CREDIT_STRONG_BEAR_OAS,
    FED_BS_MA_WEEKS,
    VIX_EXTREME_LOW,
    VIX_HIGH,
    VIX_LOW,
    VIX_PANIC,
    YIELD_FAST_MA,
    YIELD_NEUTRAL_BAND_BPS,
    YIELD_SLOW_MA,
    YIELD_STRONG_BEAR_THRESHOLD,
    YIELD_STRONG_BULL_THRESHOLD,
)

logger = logging.getLogger(__name__)


def score_yield(df: pd.DataFrame) -> int:
    """Score 10Y Treasury yield signal.

    df: DataFrame with columns [ts, value], sorted ascending.
    Returns int in [-2, +2].
    """
    if len(df) < YIELD_SLOW_MA:
        logger.warning("Insufficient data for yield signal (%d rows)", len(df))
        return 0

    series = df["value"].dropna()
    ma_fast = series.rolling(YIELD_FAST_MA).mean().iloc[-1]
    ma_slow = series.rolling(YIELD_SLOW_MA).mean().iloc[-1]
    latest = series.iloc[-1]

    diff = ma_fast - ma_slow  # negative = fast below slow = yields falling = bullish

    if abs(diff) <= YIELD_NEUTRAL_BAND_BPS:
        return 0

    if diff < 0:  # fast < slow → bullish
        if latest < YIELD_STRONG_BULL_THRESHOLD:
            return 2
        return 1
    else:  # fast > slow → bearish
        if latest > YIELD_STRONG_BEAR_THRESHOLD:
            return -2
        return -1


def score_vix(latest_vix: float) -> int:
    """Score VIX volatility signal based on absolute level.

    Note: VIX > 35 returns -2 by default. Contrarian override (+2) requires
    manual confirmation and must be applied externally.
    """
    if latest_vix < VIX_EXTREME_LOW:
        return 2
    if latest_vix < VIX_LOW:
        return 1
    if latest_vix < VIX_HIGH:
        return 0
    if latest_vix < VIX_PANIC:
        return -1
    return -2  # panic zone; contrarian override must be done manually


def score_fed_balance(df: pd.DataFrame) -> int:
    """Score Fed balance sheet signal.

    df: DataFrame with columns [ts, value] (weekly WALCL), sorted ascending.
    Returns int in [-2, +2].
    """
    if len(df) < FED_BS_MA_WEEKS + 1:
        logger.warning("Insufficient data for Fed BS signal (%d rows)", len(df))
        return 0

    series = df["value"].dropna()
    wow_change = series.diff()  # week-over-week change
    ma = wow_change.rolling(FED_BS_MA_WEEKS).mean().iloc[-1]

    if pd.isna(ma):
        return 0

    if ma > 0:
        # Expansion — magnitude determines strength
        pct_change = ma / series.iloc[-1] * 100 if series.iloc[-1] != 0 else 0
        return 2 if pct_change > 0.1 else 1
    else:
        pct_change = abs(ma) / series.iloc[-1] * 100 if series.iloc[-1] != 0 else 0
        return -2 if pct_change > 0.1 else -1


def score_credit_spread(df: pd.DataFrame) -> int:
    """Score Investment Grade credit spread (OAS) signal.

    df: DataFrame with columns [ts, value] (daily BAMLC0A0CM), sorted ascending.
    Returns int in [-2, +2].
    """
    if len(df) < CREDIT_SLOW_MA:
        logger.warning("Insufficient data for credit spread signal (%d rows)", len(df))
        return 0

    series = df["value"].dropna()
    ma_fast = series.rolling(CREDIT_FAST_MA).mean().iloc[-1]
    ma_slow = series.rolling(CREDIT_SLOW_MA).mean().iloc[-1]
    latest = series.iloc[-1]

    if ma_fast < ma_slow:  # spreads tightening → bullish
        return 1

    # Spreads widening
    if latest > CREDIT_STRONG_BEAR_OAS:  # absolute spread > 200bps AND widening
        return -2
    return -1


def compute_all_signals(
    yield_df: pd.DataFrame,
    vix_df: pd.DataFrame,
    fed_df: pd.DataFrame,
    credit_df: pd.DataFrame,
) -> dict:
    """Compute macro signals and registry-weighted composite score.

    All DataFrames must have columns [ts, value], sorted ascending.
    Returns a dict with individual scores and composite in [-8, +8].

    The composite is normalised so that disabling signals doesn't collapse
    the range — a system running 2 signals still produces a composite in
    [-8, +8] (not [-4, +4]).
    """
    from signals import registry as _reg

    latest_vix = float(vix_df["value"].dropna().iloc[-1]) if not vix_df.empty else 20.0

    # Map registry key → (DB column name, scorer callable)
    _SIGNAL_MAP = {
        "yield_10y":         ("yield_10y_signal", lambda: score_yield(yield_df)),
        "vix":               ("vix_signal",        lambda: score_vix(latest_vix)),
        "fed_balance_sheet": ("fed_bs_signal",      lambda: score_fed_balance(fed_df)),
        "credit_spread":     ("credit_spread_signal", lambda: score_credit_spread(credit_df)),
    }

    scores: dict[str, int] = {}
    weighted_sum = 0.0
    max_possible = 0.0

    for name, (col, fn) in _SIGNAL_MAP.items():
        if _reg.is_enabled("macro", name):
            w = _reg.signal_weight("macro", name)
            s = fn()
            scores[col] = s
            weighted_sum += s * w
            max_possible += 2.0 * w  # each signal's max absolute value is 2
        else:
            scores[col] = 0  # disabled signals contribute 0 to DB column

    # Normalise to [-8, +8] — same range as the original 4-signal simple sum
    composite = round(weighted_sum / max_possible * 8) if max_possible else 0
    composite = max(-8, min(8, composite))

    result = {**scores, "composite_score": composite}
    logger.info(
        "Signals — yield:%d vix:%d fed:%d credit:%d composite:%d",
        scores.get("yield_10y_signal", 0), scores.get("vix_signal", 0),
        scores.get("fed_bs_signal", 0), scores.get("credit_spread_signal", 0),
        composite,
    )
    return result
