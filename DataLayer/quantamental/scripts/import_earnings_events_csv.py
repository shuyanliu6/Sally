"""Import EPS surprise events for PEAD from a reviewed CSV."""

from __future__ import annotations

import argparse

if __package__ in (None, ""):
    from _bootstrap import add_project_root

    add_project_root(__file__)

from quantamental.config.settings import SQLITE_PATH
from quantamental.signals.earnings_importer import import_earnings_events_from_csv


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import reviewed earnings events from CSV. Dry-run by default."
    )
    parser.add_argument("--file", required=True, help="CSV file with PEAD events")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Write READY rows into SQLite. Without this flag, only prints a dry-run.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Update an existing symbol/report-date event. Default preserves existing rows.",
    )
    parser.add_argument("--db-path", default=SQLITE_PATH, help=f"SQLite DB path (default: {SQLITE_PATH})")
    args = parser.parse_args()

    report = import_earnings_events_from_csv(
        args.file,
        commit=args.commit,
        overwrite=args.overwrite,
        path=args.db_path,
    )

    mode = "COMMIT" if args.commit else "DRY RUN"
    print(f"Earnings CSV import ({mode}) file={args.file}")
    if report.empty:
        print("No rows returned.")
        return 0

    display_cols = [
        "symbol",
        "report_date",
        "fiscal_period",
        "eps_actual",
        "eps_estimate",
        "surprise_pct",
        "source",
        "status",
        "reason",
        "event_id",
    ]
    display_cols = [col for col in display_cols if col in report.columns]
    print(report[display_cols].to_string(index=False))
    counts = report["status"].value_counts().to_dict() if "status" in report else {}
    print(f"\nSummary: {counts}")
    if not args.commit:
        print("\nNo data was written. Re-run with --commit to upsert READY rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
