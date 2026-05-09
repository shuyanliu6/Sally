"""Alpha signal diagnostics.

The performance report asks whether the ranker worked. This module asks which
parts of the ranker helped or hurt the forward-return result.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from quantamental.alpha.features import DEFAULT_BENCHMARK


DEFAULT_DIAGNOSTIC_FACTORS = (
    "alpha_score",
    "stock_composite_component",
    "ema_signal_component",
    "rsi_signal_component",
    "volume_signal_component",
    "pead_signal_component",
    "momentum_20_component",
    "volatility_20_component",
    "drawdown_60_component",
    "context_macro_component",
    "context_sector_component",
)


FACTOR_LABELS = {
    "alpha_score": "Overall alpha score",
    "stock_composite_component": "Stock composite",
    "ema_signal_component": "EMA trend",
    "rsi_signal_component": "RSI",
    "volume_signal_component": "Volume",
    "pead_signal_component": "PEAD",
    "momentum_20_component": "20D momentum",
    "volatility_20_component": "Low volatility",
    "drawdown_60_component": "60D drawdown",
    "context_macro_component": "Macro context",
    "context_sector_component": "Sector context",
}


@dataclass(frozen=True)
class AlphaDiagnosticReport:
    component_summary: pd.DataFrame
    bucket_attribution: pd.DataFrame
    recommendations: pd.DataFrame


def _numeric_frame(rank_log: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    out = rank_log.copy()
    for col in cols:
        if col in out:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _mean_rank_ic(valid: pd.DataFrame, factor: str, target_col: str) -> tuple[float, float, float, int]:
    ics = []
    for _, group in valid.groupby("asof_date"):
        clean = group[[factor, target_col]].dropna()
        if len(clean) < 3 or clean[factor].nunique() <= 1:
            continue
        ic = clean[factor].rank().corr(clean[target_col].rank())
        if pd.notna(ic):
            ics.append(float(ic))
    if not ics:
        return 0.0, 0.0, 0.0, 0
    return float(np.mean(ics)), float(np.median(ics)), float((np.array(ics) > 0).mean()), len(ics)


def _top_bottom_stats(
    valid: pd.DataFrame,
    factor: str,
    target_col: str,
    quantile: float,
) -> tuple[float, float, float, float]:
    top_values = []
    bottom_values = []
    spread_values = []
    for _, group in valid.groupby("asof_date"):
        clean = group[[factor, target_col]].dropna()
        if len(clean) < 3 or clean[factor].nunique() <= 1:
            continue
        low = clean[factor].quantile(quantile)
        high = clean[factor].quantile(1.0 - quantile)
        bottom = clean[clean[factor] <= low]
        top = clean[clean[factor] >= high]
        if top.empty or bottom.empty:
            continue
        top_mean = float(top[target_col].mean())
        bottom_mean = float(bottom[target_col].mean())
        top_values.append(top_mean)
        bottom_values.append(bottom_mean)
        spread_values.append(top_mean - bottom_mean)
    if not spread_values:
        return np.nan, np.nan, np.nan, np.nan
    return (
        float(np.mean(top_values)),
        float(np.mean(bottom_values)),
        float(np.mean(spread_values)),
        float((np.array(spread_values) > 0).mean()),
    )


def _recommendation(mean_ic: float, spread: float, rank_dates: int, active_rank_dates: int, min_dates: int) -> str:
    if rank_dates < min_dates:
        return "INSUFFICIENT_HISTORY"
    if active_rank_dates == 0:
        return "NO_VARIATION"
    if active_rank_dates < min_dates:
        return "LIMITED_VARIATION"
    if mean_ic >= 0.03 and spread > 0:
        return "SUPPORTIVE"
    if mean_ic <= -0.03 and spread < 0:
        return "HARMFUL_REVIEW_WEIGHT"
    if mean_ic < 0 or spread < 0:
        return "WEAK_REVIEW"
    return "NEUTRAL"


def summarize_component_diagnostics(
    rank_log: pd.DataFrame,
    horizons: tuple[int, ...] = (20, 40),
    benchmark: str = DEFAULT_BENCHMARK,
    factors: tuple[str, ...] = DEFAULT_DIAGNOSTIC_FACTORS,
    quantile: float = 0.20,
    min_dates: int = 8,
) -> pd.DataFrame:
    """Measure each rank component against forward excess returns."""
    if rank_log.empty:
        return pd.DataFrame()
    if not 0 < quantile < 0.5:
        raise ValueError("quantile must be between 0 and 0.5")

    available = [factor for factor in factors if factor in rank_log.columns]
    target_cols = [f"fwd_{horizon}d_excess_{benchmark}" for horizon in horizons]
    working = _numeric_frame(rank_log, available + target_cols)

    rows = []
    total_rows = max(len(working), 1)
    for horizon in horizons:
        target_col = f"fwd_{horizon}d_excess_{benchmark}"
        if target_col not in working:
            continue
        for factor in available:
            valid = working.dropna(subset=[factor, target_col])
            rank_dates = int(valid["asof_date"].nunique()) if "asof_date" in valid else 0
            mean_ic, median_ic, positive_ic_rate, active_rank_dates = _mean_rank_ic(
                valid, factor, target_col
            )
            top_avg, bottom_avg, spread, spread_hit_rate = _top_bottom_stats(
                valid, factor, target_col, quantile
            )
            rows.append(
                {
                    "horizon": horizon,
                    "factor": factor,
                    "label": FACTOR_LABELS.get(factor, factor),
                    "observations": int(len(valid)),
                    "rank_dates": rank_dates,
                    "active_rank_dates": active_rank_dates,
                    "coverage": float(len(valid) / total_rows),
                    "mean_rank_ic": mean_ic,
                    "median_rank_ic": median_ic,
                    "positive_ic_rate": positive_ic_rate,
                    "top_quantile_avg_excess": top_avg,
                    "bottom_quantile_avg_excess": bottom_avg,
                    "top_minus_bottom": spread,
                    "top_minus_bottom_hit_rate": spread_hit_rate,
                    "recommendation": _recommendation(
                        mean_ic, spread, rank_dates, active_rank_dates, min_dates
                    ),
                }
            )
    return pd.DataFrame(rows)


def summarize_bucket_attribution(
    rank_log: pd.DataFrame,
    factors: tuple[str, ...] = DEFAULT_DIAGNOSTIC_FACTORS,
) -> pd.DataFrame:
    """Show which components separate TOP/BUY names from AVOID names."""
    if rank_log.empty or "bucket" not in rank_log:
        return pd.DataFrame()
    available = [factor for factor in factors if factor in rank_log.columns]
    if not available:
        return pd.DataFrame()

    working = _numeric_frame(rank_log, available)
    top = working[working["bucket"].isin(["TOP_BUY", "BUY"])]
    avoid = working[working["bucket"].eq("AVOID")]
    rows = []
    for factor in available:
        top_avg = float(top[factor].mean()) if not top.empty else np.nan
        avoid_avg = float(avoid[factor].mean()) if not avoid.empty else np.nan
        rows.append(
            {
                "factor": factor,
                "label": FACTOR_LABELS.get(factor, factor),
                "top_buy_buy_avg": top_avg,
                "avoid_avg": avoid_avg,
                "top_minus_avoid": top_avg - avoid_avg if pd.notna(top_avg) and pd.notna(avoid_avg) else np.nan,
                "all_avg": float(working[factor].mean()),
            }
        )
    return pd.DataFrame(rows)


def build_alpha_diagnostic_report(
    rank_log: pd.DataFrame,
    horizons: tuple[int, ...] = (20, 40),
    benchmark: str = DEFAULT_BENCHMARK,
    factors: tuple[str, ...] = DEFAULT_DIAGNOSTIC_FACTORS,
    quantile: float = 0.20,
    min_dates: int = 8,
) -> AlphaDiagnosticReport:
    component_summary = summarize_component_diagnostics(
        rank_log=rank_log,
        horizons=horizons,
        benchmark=benchmark,
        factors=factors,
        quantile=quantile,
        min_dates=min_dates,
    )
    recommendations = (
        component_summary[
            [
                "horizon",
                "factor",
                "label",
                "mean_rank_ic",
                "top_minus_bottom",
                "positive_ic_rate",
                "active_rank_dates",
                "recommendation",
            ]
        ].copy()
        if not component_summary.empty
        else pd.DataFrame()
    )
    if not recommendations.empty:
        recommendations = recommendations.sort_values(
            ["horizon", "recommendation", "top_minus_bottom"],
            ascending=[True, True, False],
        ).reset_index(drop=True)

    return AlphaDiagnosticReport(
        component_summary=component_summary,
        bucket_attribution=summarize_bucket_attribution(rank_log, factors=factors),
        recommendations=recommendations,
    )
