"""V1 alpha backtest engine."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from quantamental.alpha.features import DEFAULT_BENCHMARK, add_forward_returns, build_features
from quantamental.alpha.portfolio import construct_portfolio
from quantamental.alpha.ranking import rank_alpha


@dataclass(frozen=True)
class BacktestResult:
    metrics: pd.DataFrame
    daily_returns: pd.DataFrame
    rebalance_log: pd.DataFrame


def _metrics(returns: pd.Series) -> dict:
    clean = returns.dropna()
    if clean.empty:
        return {
            "cagr": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "max_drawdown": 0.0,
            "calmar": 0.0,
        }
    equity = (1.0 + clean).cumprod()
    years = max(len(clean) / 252.0, 1 / 252)
    cagr = equity.iloc[-1] ** (1.0 / years) - 1.0
    vol = clean.std() * np.sqrt(252)
    downside = clean[clean < 0].std() * np.sqrt(252)
    drawdown = equity / equity.cummax() - 1.0
    max_dd = float(drawdown.min())
    return {
        "cagr": float(cagr),
        "sharpe": float(clean.mean() * 252 / vol) if vol else 0.0,
        "sortino": float(clean.mean() * 252 / downside) if downside else 0.0,
        "max_drawdown": max_dd,
        "calmar": float(cagr / abs(max_dd)) if max_dd else 0.0,
    }


def _rebalance_dates(dates: pd.DatetimeIndex, start, end) -> list[pd.Timestamp]:
    window = dates[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
    if len(window) == 0:
        return []
    first_by_week = pd.Series(window, index=window).groupby(window.to_period("W")).first()
    return list(pd.to_datetime(first_by_week.values))


def _information_coefficient(log: pd.DataFrame) -> float:
    if log.empty or "fwd_20d_excess_SMH" not in log:
        return 0.0
    ics = []
    for _, group in log.groupby("asof_date"):
        valid = group[["alpha_score", "fwd_20d_excess_SMH"]].dropna()
        if len(valid) >= 3 and valid["alpha_score"].nunique() > 1:
            ic = valid["alpha_score"].rank().corr(valid["fwd_20d_excess_SMH"].rank())
            if pd.notna(ic):
                ics.append(ic)
    return float(np.mean(ics)) if ics else 0.0


def run_backtest(
    ohlcv: pd.DataFrame,
    stock_signals: pd.DataFrame,
    regime_signals: pd.DataFrame,
    sector_signals: pd.DataFrame,
    symbols: list[str],
    start,
    end,
    top_n: int = 10,
    transaction_cost_bps: float = 15.0,
    benchmark: str = DEFAULT_BENCHMARK,
) -> BacktestResult:
    """Run a weekly top-N rank strategy with next-day execution."""
    prices = ohlcv.copy()
    prices["ts"] = pd.to_datetime(prices["ts"])
    if getattr(prices["ts"].dt, "tz", None) is not None:
        prices["ts"] = prices["ts"].dt.tz_convert(None)
    pivot = prices.pivot_table(index="ts", columns="symbol", values="close", aggfunc="last").sort_index()
    candidate_symbols = [s for s in symbols if s in pivot.columns]
    if not candidate_symbols:
        empty = pd.DataFrame()
        return BacktestResult(metrics=empty, daily_returns=empty, rebalance_log=empty)

    returns = pivot[candidate_symbols].pct_change().fillna(0.0)
    all_dates = pivot.index
    rebalances = set(_rebalance_dates(all_dates, start, end))
    active_dates = all_dates[(all_dates >= pd.Timestamp(start)) & (all_dates <= pd.Timestamp(end))]

    weights = pd.Series(0.0, index=candidate_symbols)
    pending_weights: pd.Series | None = None
    strategy_returns = []
    turnover_values = []
    rebalance_rows = []
    cost_rate = transaction_cost_bps / 10000.0

    for current_date in active_dates:
        if pending_weights is not None:
            turnover = float((pending_weights - weights).abs().sum())
            weights = pending_weights
            pending_weights = None
        else:
            turnover = 0.0

        day_return = float((returns.loc[current_date, candidate_symbols] * weights).sum())
        day_return -= turnover * cost_rate
        strategy_returns.append({"ts": current_date, "alpha_strategy": day_return, "turnover": turnover})
        turnover_values.append(turnover)

        if current_date in rebalances:
            features = build_features(
                ohlcv=ohlcv,
                stock_signals=stock_signals,
                regime_signals=regime_signals,
                sector_signals=sector_signals,
                symbols=candidate_symbols,
                asof=current_date,
                benchmark=benchmark,
            )
            ranks = rank_alpha(features)
            portfolio = construct_portfolio(ranks, top_n=top_n)
            labels = add_forward_returns(portfolio, ohlcv, horizons=(20, 40), benchmark=benchmark)
            rebalance_rows.append(labels)
            pending_weights = (
                portfolio.set_index("symbol")["target_weight"]
                .reindex(candidate_symbols)
                .fillna(0.0)
            )

    daily = pd.DataFrame(strategy_returns).set_index("ts")
    daily["equal_weight_candidates"] = returns.loc[daily.index, candidate_symbols].mean(axis=1)
    for base in ["SPY", "QQQ", benchmark]:
        daily[base] = pivot[base].pct_change().reindex(daily.index).fillna(0.0) if base in pivot else 0.0

    log = pd.concat(rebalance_rows, ignore_index=True) if rebalance_rows else pd.DataFrame()
    metrics_rows = []
    for name in ["alpha_strategy", "equal_weight_candidates", "SPY", "QQQ", benchmark]:
        row = {"strategy": name, **_metrics(daily[name])}
        metrics_rows.append(row)
    metrics = pd.DataFrame(metrics_rows)
    metrics["avg_turnover"] = float(np.mean(turnover_values)) if turnover_values else 0.0
    metrics["hit_rate"] = float((daily["alpha_strategy"] > 0).mean()) if not daily.empty else 0.0
    metrics["information_coefficient"] = _information_coefficient(log)
    metrics["avg_holding_period_days"] = 5.0
    return BacktestResult(metrics=metrics, daily_returns=daily.reset_index(), rebalance_log=log)

