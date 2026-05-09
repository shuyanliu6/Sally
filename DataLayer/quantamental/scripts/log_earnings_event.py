"""Log EPS surprise events for PEAD."""

from __future__ import annotations

import argparse
from datetime import date

if __package__ in (None, ""):
    from _bootstrap import add_project_root

    add_project_root(__file__)

from quantamental.config.settings import SQLITE_PATH
from quantamental.signals.earnings import load_earnings_events, log_earnings_event


def _date_arg(value: str) -> date:
    return date.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Log or inspect earnings events for PEAD")
    parser.add_argument("--show", action="store_true", help="Show logged earnings events")
    parser.add_argument("--symbol", help="Ticker symbol")
    parser.add_argument("--report-date", type=_date_arg, help="Earnings report date YYYY-MM-DD")
    parser.add_argument("--fiscal-period", help="Fiscal period label, e.g. 2026-Q1")
    parser.add_argument("--surprise-pct", type=float, help="EPS surprise in percent points, e.g. 12.5")
    parser.add_argument("--eps-actual", type=float, help="Actual EPS")
    parser.add_argument("--eps-estimate", type=float, help="Consensus EPS estimate")
    parser.add_argument("--source", help="Data source, e.g. company release, Yahoo, FactSet")
    parser.add_argument("--notes", help="Optional operator note")
    parser.add_argument("--db-path", default=SQLITE_PATH, help=f"SQLite DB path (default: {SQLITE_PATH})")
    args = parser.parse_args()

    if args.show:
        events = load_earnings_events(path=args.db_path)
        if events.empty:
            print("No earnings events logged.")
        else:
            print(events.to_string(index=False))
        return 0

    if not args.symbol or not args.report_date:
        parser.error("--symbol and --report-date are required unless --show is used")

    event_id = log_earnings_event(
        symbol=args.symbol,
        report_date=args.report_date,
        fiscal_period=args.fiscal_period,
        surprise_pct=args.surprise_pct,
        eps_actual=args.eps_actual,
        eps_estimate=args.eps_estimate,
        source=args.source,
        notes=args.notes,
        path=args.db_path,
    )
    events = load_earnings_events(symbols=[args.symbol], path=args.db_path)
    row = events[events["id"].eq(event_id)].iloc[0]
    print(
        "Logged earnings event "
        f"id={event_id} {row['symbol']} report_date={row['report_date']} "
        f"surprise={float(row['surprise_pct']):+.1f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
