"""Diagnose which alpha components help or hurt forward returns."""

from __future__ import annotations

import argparse
from datetime import date

if __package__ in (None, ""):
    from _bootstrap import add_project_root

    add_project_root(__file__)

import pandas as pd

from quantamental.alpha.diagnostics import AlphaDiagnosticReport, build_alpha_diagnostic_report
from quantamental.alpha.features import load_backtest_inputs_from_questdb
from quantamental.alpha.performance import build_performance_report
from quantamental.alpha.reporting import save_alpha_diagnostic_report
from quantamental.config.universe import load_candidate_list


def _date_arg(value: str) -> date:
    return date.fromisoformat(value)


def _window_arg(value: str) -> tuple[date, date]:
    raw = value.replace(",", ":")
    parts = [part.strip() for part in raw.split(":") if part.strip()]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("window must be START:END, e.g. 2025-01-01:2026-04-01")
    start, end = (_date_arg(parts[0]), _date_arg(parts[1]))
    if start >= end:
        raise argparse.ArgumentTypeError("window start must be before end")
    return start, end


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose alpha component forward performance")
    parser.add_argument("--start", type=_date_arg, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=_date_arg, help="End date YYYY-MM-DD")
    parser.add_argument(
        "--window",
        action="append",
        type=_window_arg,
        help="Diagnostic window START:END. Can be repeated for multi-window validation.",
    )
    parser.add_argument("--top-n", type=int, default=10, help="Target top-N holdings (default: 10)")
    parser.add_argument(
        "--frequency",
        choices=["weekly", "daily"],
        default="weekly",
        help="Ranking evaluation frequency (default: weekly)",
    )
    parser.add_argument(
        "--quantile",
        type=float,
        default=0.20,
        help="Top/bottom factor slice for spread diagnostics (default: 0.20)",
    )
    parser.add_argument("--no-save", action="store_true", help="Do not save diagnostic CSVs")
    args = parser.parse_args()

    windows = args.window or []
    if args.start or args.end:
        if not args.start or not args.end:
            parser.error("--start and --end must be provided together")
        if args.start >= args.end:
            parser.error("--start must be before --end")
        windows.append((args.start, args.end))
    if not windows:
        parser.error("provide --start/--end or at least one --window START:END")

    symbols = load_candidate_list()
    reports = []
    for start, end in windows:
        inputs = load_backtest_inputs_from_questdb(symbols=symbols, start=start, end=end)
        performance = build_performance_report(
            ohlcv=inputs.ohlcv,
            stock_signals=inputs.stock_signals,
            regime_signals=inputs.regime_signals,
            sector_signals=inputs.sector_signals,
            earnings_events=inputs.earnings_events,
            symbols=symbols,
            start=start,
            end=end,
            top_n=args.top_n,
            frequency=args.frequency,
        )
        if performance.rank_log.empty:
            print(f"No rank log produced for {start}:{end}. Check data coverage.")
            continue
        report = build_alpha_diagnostic_report(performance.rank_log, quantile=args.quantile)
        window_label = f"{start}:{end}"
        for frame in (report.component_summary, report.bucket_attribution, report.recommendations):
            if not frame.empty:
                frame.insert(0, "window", window_label)
        reports.append(report)

    if not reports:
        print("No alpha diagnostics produced. Check OHLCV and signal coverage.")
        return 1

    report = AlphaDiagnosticReport(
        component_summary=pd.concat([r.component_summary for r in reports], ignore_index=True),
        bucket_attribution=pd.concat([r.bucket_attribution for r in reports], ignore_index=True),
        recommendations=pd.concat([r.recommendations for r in reports], ignore_index=True),
    )
    if report.component_summary.empty:
        print("No alpha diagnostics produced. Check rank log columns and forward-return labels.")
        return 1

    print("\nComponent diagnostics")
    cols = [
        "window",
        "horizon",
        "label",
        "mean_rank_ic",
        "top_minus_bottom",
        "positive_ic_rate",
        "active_rank_dates",
        "recommendation",
    ]
    print(report.component_summary[cols].to_string(index=False))

    print("\nBucket attribution")
    attr_cols = ["window", "label", "top_buy_buy_avg", "avoid_avg", "top_minus_avoid"]
    print(report.bucket_attribution[attr_cols].to_string(index=False))

    if not args.no_save:
        paths = save_alpha_diagnostic_report(report)
        print(f"\nSaved diagnostic recommendations: {paths['latest_recommendations']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
