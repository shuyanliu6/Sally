"""Generate alpha bucket forward-performance reports."""

from __future__ import annotations

import argparse
from datetime import date

if __package__ in (None, ""):
    from _bootstrap import add_project_root

    add_project_root(__file__)

from quantamental.alpha.features import load_backtest_inputs_from_questdb
from quantamental.alpha.performance import build_performance_report
from quantamental.alpha.reporting import save_alpha_performance_report
from quantamental.config.universe import load_candidate_list


def _date_arg(value: str) -> date:
    return date.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate alpha forward-performance report")
    parser.add_argument("--start", required=True, type=_date_arg, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, type=_date_arg, help="End date YYYY-MM-DD")
    parser.add_argument("--top-n", type=int, default=10, help="Target top-N holdings (default: 10)")
    parser.add_argument(
        "--frequency",
        choices=["weekly", "daily"],
        default="weekly",
        help="Ranking evaluation frequency (default: weekly)",
    )
    parser.add_argument("--no-save", action="store_true", help="Do not save report CSVs")
    args = parser.parse_args()

    symbols = load_candidate_list()
    inputs = load_backtest_inputs_from_questdb(symbols=symbols, start=args.start, end=args.end)
    report = build_performance_report(
        ohlcv=inputs.ohlcv,
        stock_signals=inputs.stock_signals,
        regime_signals=inputs.regime_signals,
        sector_signals=inputs.sector_signals,
        symbols=symbols,
        start=args.start,
        end=args.end,
        top_n=args.top_n,
        frequency=args.frequency,
    )
    if report.headline.empty:
        print("No alpha performance report produced. Check data coverage and forward-return window.")
        return 1

    print("\nHeadline")
    print(report.headline.to_string(index=False))
    print("\nBucket summary")
    print(report.bucket_summary.to_string(index=False))

    if not args.no_save:
        paths = save_alpha_performance_report(report)
        print(f"\nSaved headline report: {paths['latest_headline']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
