"""
Manual entry CLI for Hyperscaler Capex Surprise (Signal C).

After each quarterly earnings release for META / MSFT / GOOGL / AMZN,
log actual vs consensus capex. Signal C activates when ≥2 companies have reported
for the same quarter.

Usage:
    python scripts/log_capex.py --quarter 2026-Q1 --company META --actual 8.2 --consensus 7.5
    python scripts/log_capex.py --show
    python scripts/log_capex.py --score 2026-Q1
"""

import argparse
import logging
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import SQLITE_PATH
from signals.sector_ai_infra import (
    CAPEX_TRACKED_COMPANIES,
    add_capex_surprise,
    calc_capex_signal_for_quarter,
    init_ai_infra_db,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def cmd_show(path: str = SQLITE_PATH):
    init_ai_infra_db(path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM capex_surprise ORDER BY quarter DESC, company"
    ).fetchall()
    con.close()

    if not rows:
        print("No capex data yet. Add with --quarter --company --actual --consensus.")
        return

    print(f"\n{'Quarter':<10} {'Company':<7} {'Actual ($B)':>12} {'Consensus':>11} {'Surprise':>10}")
    print("─" * 60)
    for r in rows:
        print(f"{r['quarter']:<10} {r['company']:<7} "
              f"{r['actual_capex_bn']:>12.2f} "
              f"{r['consensus_capex_bn']:>11.2f} "
              f"{r['surprise_pct']*100:>+9.1f}%")
    print()


def cmd_score(quarter: str):
    score = calc_capex_signal_for_quarter(quarter)
    print(f"\nSignal C (Capex Surprise) for {quarter}: {score:+d}")
    print("   +2: avg beat > 10%   +1: avg beat > 0%   -1: avg miss < 10%   -2: avg miss > 10%")


def main():
    p = argparse.ArgumentParser(description="Log hyperscaler capex (Signal C)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--show",   action="store_true", help="Print all logged capex data")
    g.add_argument("--score",  metavar="QUARTER",   help="Compute Signal C for a quarter")
    g.add_argument("--quarter", help="Quarter, e.g. 2026-Q1 (use with --company etc.)")

    p.add_argument("--company",   choices=CAPEX_TRACKED_COMPANIES,
                   help="META, MSFT, GOOGL, or AMZN")
    p.add_argument("--actual",    type=float, help="Actual capex in USD billions")
    p.add_argument("--consensus", type=float, help="Consensus capex in USD billions")

    args = p.parse_args()

    if args.show:
        cmd_show()
        return
    if args.score:
        cmd_score(args.score)
        return

    if not (args.company and args.actual is not None and args.consensus is not None):
        p.error("--company, --actual, --consensus all required when --quarter is set")

    add_capex_surprise(args.quarter, args.company, args.actual, args.consensus)
    print(f"\n✅ Logged capex for {args.company} in {args.quarter}")
    cmd_score(args.quarter)


if __name__ == "__main__":
    main()
