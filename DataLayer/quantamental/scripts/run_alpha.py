"""Run the V1 alpha ranker for the AI-infra candidate universe."""

from __future__ import annotations

import argparse
from datetime import date

import pandas as pd

if __package__ in (None, ""):
    from _bootstrap import add_project_root

    add_project_root(__file__)

from quantamental.alpha.features import build_features, load_feature_inputs_from_questdb
from quantamental.alpha.portfolio import construct_portfolio
from quantamental.alpha.ranking import rank_alpha
from quantamental.alpha.reporting import (
    persist_alpha_ranks_to_questdb,
    save_alpha_ranks,
)
from quantamental.config.universe import load_equity_candidate_list


def _parse_date(value: str | None):
    if value:
        return date.fromisoformat(value)
    from quantamental.dashboard.freshness import expected_market_date

    return expected_market_date()


def run(asof=None, persist_db: bool = False, save: bool = True, top_n: int = 10) -> pd.DataFrame:
    symbols = load_equity_candidate_list()
    inputs = load_feature_inputs_from_questdb(symbols=symbols, asof=asof)
    features = build_features(
        ohlcv=inputs.ohlcv,
        stock_signals=inputs.stock_signals,
        regime_signals=inputs.regime_signals,
        sector_signals=inputs.sector_signals,
        earnings_events=inputs.earnings_events,
        symbols=symbols,
        asof=asof,
    )
    ranks = construct_portfolio(rank_alpha(features), top_n=top_n)
    if save:
        paths = save_alpha_ranks(ranks)
        print(f"Saved alpha ranks: {paths['latest']}")
    if persist_db:
        count = persist_alpha_ranks_to_questdb(ranks)
        print(f"Persisted {count} alpha rank rows to QuestDB")
    return ranks


def main() -> int:
    parser = argparse.ArgumentParser(description="Run V1 alpha ranking")
    parser.add_argument("--asof", help="As-of date YYYY-MM-DD (default: latest expected US market date)")
    parser.add_argument("--top-n", type=int, default=10, help="Target top-N holdings (default: 10)")
    parser.add_argument("--no-save", action="store_true", help="Do not save Parquet/CSV ranking artifacts")
    parser.add_argument(
        "--persist-db",
        action="store_true",
        help="Also persist alpha_ranks to QuestDB (off by default)",
    )
    args = parser.parse_args()

    ranks = run(
        asof=_parse_date(args.asof),
        persist_db=args.persist_db,
        save=not args.no_save,
        top_n=args.top_n,
    )
    if ranks.empty:
        print("No alpha ranks produced.")
        return 1

    cols = ["rank", "symbol", "bucket", "alpha_score", "target_weight", "target_cash"]
    print("\nTop candidates")
    print(ranks[cols].head(12).to_string(index=False))
    print("\nAvoid / bottom candidates")
    print(ranks[cols].tail(8).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
