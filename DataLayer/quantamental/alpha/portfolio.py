"""Portfolio construction from alpha ranks."""

from __future__ import annotations

import pandas as pd


def deployment_cap(macro_score: float, macro_regime: str, sector_score: float) -> float:
    """Return max deployed capital based on macro and sector context."""
    cap = 1.0
    if macro_regime == "RISK_OFF" or macro_score < -4:
        cap = min(cap, 0.50)
    if sector_score < 0:
        cap = min(cap, 0.70)
    return cap


def construct_portfolio(
    ranks: pd.DataFrame,
    top_n: int = 10,
    min_names: int = 8,
    max_names: int = 12,
    max_weight: float = 0.15,
    min_weight: float = 0.05,
) -> pd.DataFrame:
    """Convert ranks into long-only target weights."""
    if ranks.empty:
        return ranks.copy()

    df = ranks.copy()
    macro_score = float(df.get("macro_score", pd.Series([0])).iloc[0] or 0)
    sector_score = float(df.get("sector_score", pd.Series([0])).iloc[0] or 0)
    macro_regime = str(df.get("macro_regime", pd.Series(["UNKNOWN"])).iloc[0] or "UNKNOWN")
    cap = deployment_cap(macro_score, macro_regime, sector_score)

    target_count = max(min_names, min(max_names, top_n))
    eligible = df[df["bucket"].isin(["TOP_BUY", "BUY", "HOLD"])].sort_values("rank")
    if macro_regime == "RISK_OFF" or macro_score < -4:
        eligible = eligible[eligible["bucket"].eq("TOP_BUY")]

    max_count_for_min_weight = int(cap / min_weight) if min_weight > 0 else target_count
    count = max(0, min(target_count, max_count_for_min_weight, len(eligible)))
    selected_symbols = set(eligible.head(count)["symbol"])

    df["target_weight"] = 0.0
    if selected_symbols:
        equal_weight = min(max_weight, cap / len(selected_symbols))
        df.loc[df["symbol"].isin(selected_symbols), "target_weight"] = equal_weight

    deployed = float(df["target_weight"].sum())
    df["target_cash"] = round(max(0.0, 1.0 - deployed), 6)
    df["deployment_cap"] = cap
    df["new_buys_allowed"] = not (macro_regime == "RISK_OFF" or macro_score < -4)
    return df.sort_values(["target_weight", "rank", "symbol"], ascending=[False, True, True]).reset_index(drop=True)


def is_weekly_rebalance_day(ts) -> bool:
    """Weekly rebalance default: Monday, or first available trading day in a backtest week."""
    return pd.Timestamp(ts).weekday() == 0

