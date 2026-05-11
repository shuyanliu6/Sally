"""Import EPS surprise events for PEAD from external providers."""

from __future__ import annotations

import argparse

if __package__ in (None, ""):
    from _bootstrap import add_project_root

    add_project_root(__file__)

from quantamental.config.settings import SQLITE_PATH
from quantamental.config.universe import load_equity_candidate_list
from quantamental.signals.earnings_importer import (
    default_import_window,
    fetch_fmp_earnings_events,
    fetch_yfinance_earnings_events,
    import_earnings_events,
)


def main() -> int:
    default_start, default_end = default_import_window()
    parser = argparse.ArgumentParser(
        description="Import recent earnings events for PEAD. Dry-run by default."
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        help="Ticker symbols to import. Defaults to the active single-name equity candidate list.",
    )
    parser.add_argument(
        "--from",
        dest="start",
        default=default_start.isoformat(),
        help=f"Start report date YYYY-MM-DD (default: {default_start})",
    )
    parser.add_argument(
        "--to",
        dest="end",
        default=default_end.isoformat(),
        help=f"End report date YYYY-MM-DD (default: {default_end})",
    )
    parser.add_argument(
        "--provider",
        choices=["yfinance", "fmp"],
        default="yfinance",
        help="Data provider to fetch from (default: yfinance).",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Write READY events into SQLite. Without this flag, only prints a dry-run.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Update an existing symbol/report-date event. Default preserves existing rows.",
    )
    parser.add_argument("--db-path", default=SQLITE_PATH, help=f"SQLite DB path (default: {SQLITE_PATH})")
    args = parser.parse_args()

    symbols = args.tickers or load_equity_candidate_list()
    fetcher = fetch_fmp_earnings_events if args.provider == "fmp" else fetch_yfinance_earnings_events
    report = import_earnings_events(
        symbols=symbols,
        start=args.start,
        end=args.end,
        commit=args.commit,
        overwrite=args.overwrite,
        path=args.db_path,
        fetcher=fetcher,
    )

    mode = "COMMIT" if args.commit else "DRY RUN"
    print(
        f"Earnings event import ({mode}) {args.start} -> {args.end} "
        f"| provider={args.provider} | symbols={len(symbols)}"
    )
    if report.empty:
        print("No rows returned.")
        return 0

    display_cols = [
        "symbol",
        "report_date",
        "eps_actual",
        "eps_estimate",
        "surprise_pct",
        "status",
        "reason",
        "event_id",
    ]
    display_cols = [col for col in display_cols if col in report.columns]
    print(report[display_cols].to_string(index=False))

    counts = report["status"].value_counts().to_dict() if "status" in report else {}
    print(f"\nSummary: {counts}")
    if not args.commit:
        print("\nNo data was written. Re-run with --commit to upsert READY events.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
