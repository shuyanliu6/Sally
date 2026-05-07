"""Backtest the V1 alpha ranker."""

from __future__ import annotations

import argparse
from datetime import date

if __package__ in (None, ""):
    from _bootstrap import add_project_root

    add_project_root(__file__)

from quantamental.alpha.backtest import run_backtest
from quantamental.alpha.features import load_backtest_inputs_from_questdb
from quantamental.alpha.reporting import save_backtest_report
from quantamental.config.universe import load_candidate_list


def _date_arg(value: str) -> date:
    return date.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest V1 alpha ranking")
    parser.add_argument("--start", required=True, type=_date_arg, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, type=_date_arg, help="End date YYYY-MM-DD")
    parser.add_argument("--top-n", type=int, default=10, help="Target top-N holdings (default: 10)")
    parser.add_argument(
        "--cost-bps",
        type=float,
        default=15.0,
        help="One-way transaction cost/slippage in bps (default: 15)",
    )
    parser.add_argument("--no-save", action="store_true", help="Do not save report CSVs")
    args = parser.parse_args()

    symbols = load_candidate_list()
    inputs = load_backtest_inputs_from_questdb(symbols=symbols, start=args.start, end=args.end)
    result = run_backtest(
        ohlcv=inputs.ohlcv,
        stock_signals=inputs.stock_signals,
        regime_signals=inputs.regime_signals,
        sector_signals=inputs.sector_signals,
        symbols=symbols,
        start=args.start,
        end=args.end,
        top_n=args.top_n,
        transaction_cost_bps=args.cost_bps,
    )

    if result.metrics.empty:
        print("No backtest metrics produced. Check OHLCV coverage for the requested window.")
        return 1

    print(result.metrics.to_string(index=False))
    if not args.no_save:
        paths = save_backtest_report(result)
        print(f"\nSaved backtest metrics: {paths['metrics']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

