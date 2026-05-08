"""Alpha performance reporting.

This module answers the fund-manager question: did the rank buckets actually
outperform the relevant benchmark after the signal date?
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from quantamental.alpha.features import DEFAULT_BENCHMARK, add_forward_returns, build_features
from quantamental.alpha.portfolio import construct_portfolio
from quantamental.alpha.ranking import rank_alpha


@dataclass(frozen=True)
class AlphaPerformanceReport:
    rank_log: pd.DataFrame
    bucket_summary: pd.DataFrame
    headline: pd.DataFrame


def _normalize_ts(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "ts" not in df:
        return df.copy()
    out = df.copy()
    out["ts"] = pd.to_datetime(out["ts"])
    if getattr(out["ts"].dt, "tz", None) is not None:
        out["ts"] = out["ts"].dt.tz_convert(None)
    return out


def _evaluation_dates(ohlcv: pd.DataFrame, start, end, benchmark: str, frequency: str) -> list[pd.Timestamp]:
    prices = _normalize_ts(ohlcv)
    if prices.empty:
        return []
    if benchmark in set(prices["symbol"]):
        dates = prices.loc[prices["symbol"].eq(benchmark), "ts"].drop_duplicates().sort_values()
    else:
        dates = prices["ts"].drop_duplicates().sort_values()
    window = pd.DatetimeIndex(dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))])
    if len(window) == 0:
        return []
    if frequency == "daily":
        return list(window)
    if frequency != "weekly":
        raise ValueError("frequency must be 'weekly' or 'daily'")
    first_by_week = pd.Series(window, index=window).groupby(window.to_period("W")).first()
    return list(pd.to_datetime(first_by_week.values))


def _candidate_equal_weight_return(
    ohlcv: pd.DataFrame,
    symbols: list[str],
    asof_date,
    horizon: int,
) -> float:
    features = pd.DataFrame({"symbol": symbols, "asof_date": pd.Timestamp(asof_date).date().isoformat()})
    labeled = add_forward_returns(features, ohlcv, horizons=(horizon,), benchmark=DEFAULT_BENCHMARK)
    col = f"fwd_{horizon}d_return"
    return float(labeled[col].mean()) if col in labeled and labeled[col].notna().any() else np.nan


def build_rank_log(
    ohlcv: pd.DataFrame,
    stock_signals: pd.DataFrame,
    regime_signals: pd.DataFrame,
    sector_signals: pd.DataFrame,
    symbols: list[str],
    start,
    end,
    top_n: int = 10,
    horizons: tuple[int, ...] = (20, 40),
    benchmark: str = DEFAULT_BENCHMARK,
    frequency: str = "weekly",
    earnings_events: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build historical rank rows with forward-return labels."""
    dates = _evaluation_dates(ohlcv, start, end, benchmark, frequency)
    rows = []
    symbols = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
    for asof in dates:
        features = build_features(
            ohlcv=ohlcv,
            stock_signals=stock_signals,
            regime_signals=regime_signals,
            sector_signals=sector_signals,
            earnings_events=earnings_events,
            symbols=symbols,
            asof=asof,
            benchmark=benchmark,
        )
        ranks = construct_portfolio(rank_alpha(features), top_n=top_n)
        labeled = add_forward_returns(ranks, ohlcv, horizons=horizons, benchmark=benchmark)
        for horizon in horizons:
            labeled[f"equal_weight_{horizon}d_return"] = _candidate_equal_weight_return(
                ohlcv, symbols, asof, horizon
            )
            labeled[f"fwd_{horizon}d_excess_equal_weight"] = (
                labeled[f"fwd_{horizon}d_return"] - labeled[f"equal_weight_{horizon}d_return"]
            )
        rows.append(labeled)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def summarize_buckets(
    rank_log: pd.DataFrame,
    horizons: tuple[int, ...] = (20, 40),
    benchmark: str = DEFAULT_BENCHMARK,
) -> pd.DataFrame:
    """Summarize forward performance by alpha bucket and horizon."""
    if rank_log.empty:
        return pd.DataFrame()
    summaries = []
    bucket_order = ["TOP_BUY", "BUY", "HOLD", "AVOID"]
    for horizon in horizons:
        ret_col = f"fwd_{horizon}d_return"
        bench_col = f"fwd_{horizon}d_excess_{benchmark}"
        ew_col = f"fwd_{horizon}d_excess_equal_weight"
        for bucket in bucket_order:
            group = rank_log[rank_log["bucket"].eq(bucket)]
            valid = group.dropna(subset=[ret_col, bench_col])
            if valid.empty:
                continue
            summaries.append(
                {
                    "horizon": horizon,
                    "bucket": bucket,
                    "n": int(len(valid)),
                    "avg_forward_return": float(valid[ret_col].mean()),
                    f"avg_excess_{benchmark}": float(valid[bench_col].mean()),
                    "avg_excess_equal_weight": float(valid[ew_col].mean()) if ew_col in valid else np.nan,
                    f"win_rate_vs_{benchmark}": float((valid[bench_col] > 0).mean()),
                    "win_rate_vs_equal_weight": float((valid[ew_col] > 0).mean()) if ew_col in valid else np.nan,
                    "hit_rate_positive": float((valid[ret_col] > 0).mean()),
                    "median_excess": float(valid[bench_col].median()),
                    "avg_alpha_score": float(valid["alpha_score"].mean()),
                }
            )
    return pd.DataFrame(summaries)


def headline_summary(
    rank_log: pd.DataFrame,
    horizons: tuple[int, ...] = (20, 40),
    benchmark: str = DEFAULT_BENCHMARK,
) -> pd.DataFrame:
    """Compact top-vs-rest and information coefficient summary."""
    if rank_log.empty:
        return pd.DataFrame()
    rows = []
    for horizon in horizons:
        excess_col = f"fwd_{horizon}d_excess_{benchmark}"
        valid = rank_log.dropna(subset=[excess_col, "alpha_score"])
        if valid.empty:
            continue
        top = valid[valid["bucket"].isin(["TOP_BUY", "BUY"])]
        avoid = valid[valid["bucket"].eq("AVOID")]
        ic_values = []
        for _, group in valid.groupby("asof_date"):
            if len(group) >= 3 and group["alpha_score"].nunique() > 1:
                ic = group["alpha_score"].rank().corr(group[excess_col].rank())
                if pd.notna(ic):
                    ic_values.append(ic)
        rows.append(
            {
                "horizon": horizon,
                "observations": int(len(valid)),
                "top_buy_buy_avg_excess": float(top[excess_col].mean()) if not top.empty else np.nan,
                "avoid_avg_excess": float(avoid[excess_col].mean()) if not avoid.empty else np.nan,
                "top_minus_avoid": (
                    float(top[excess_col].mean() - avoid[excess_col].mean())
                    if not top.empty and not avoid.empty
                    else np.nan
                ),
                "mean_rank_ic": float(np.mean(ic_values)) if ic_values else 0.0,
                "rank_dates": int(valid["asof_date"].nunique()),
            }
        )
    return pd.DataFrame(rows)


def build_performance_report(
    ohlcv: pd.DataFrame,
    stock_signals: pd.DataFrame,
    regime_signals: pd.DataFrame,
    sector_signals: pd.DataFrame,
    symbols: list[str],
    start,
    end,
    top_n: int = 10,
    horizons: tuple[int, ...] = (20, 40),
    benchmark: str = DEFAULT_BENCHMARK,
    frequency: str = "weekly",
    earnings_events: pd.DataFrame | None = None,
) -> AlphaPerformanceReport:
    rank_log = build_rank_log(
        ohlcv=ohlcv,
        stock_signals=stock_signals,
        regime_signals=regime_signals,
        sector_signals=sector_signals,
        earnings_events=earnings_events,
        symbols=symbols,
        start=start,
        end=end,
        top_n=top_n,
        horizons=horizons,
        benchmark=benchmark,
        frequency=frequency,
    )
    return AlphaPerformanceReport(
        rank_log=rank_log,
        bucket_summary=summarize_buckets(rank_log, horizons=horizons, benchmark=benchmark),
        headline=headline_summary(rank_log, horizons=horizons, benchmark=benchmark),
    )
