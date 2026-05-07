"""
Manual entry CLI for TSMC monthly revenue (Signal B).

TSMC publishes revenue ~10th of the next month at https://investor.tsmc.com/.
Run this script monthly to keep Signal B fresh.

Usage:
    python scripts/log_tsmc_revenue.py --month 2026-04 --revenue 285.7
    python scripts/log_tsmc_revenue.py --show
"""

import argparse
import logging
import os
import sqlite3
import sys

if __package__ in (None, ""):
    from _bootstrap import add_project_root
    add_project_root(__file__)

from quantamental.config.settings import SQLITE_PATH
from quantamental.signals.sector_ai_infra import add_tsmc_revenue, init_ai_infra_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def cmd_show(path: str = SQLITE_PATH):
    init_ai_infra_db(path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM tsmc_revenue ORDER BY month DESC LIMIT 24"
    ).fetchall()
    con.close()

    if not rows:
        print("No TSMC revenue data yet. Add some with --month and --revenue.")
        return

    print(f"\n{'Month':<10} {'Revenue (TWD bn)':>18} {'YoY %':>10} {'3M MA YoY':>10} {'Signal':>7}")
    print("─" * 65)
    for r in rows:
        yoy = f"{r['yoy_growth']:+.1f}%" if r["yoy_growth"] is not None else "n/a"
        ma3 = f"{r['ma3_yoy']:+.1f}%" if r["ma3_yoy"] is not None else "n/a"
        print(f"{r['month']:<10} {r['revenue_twd_bn']:>18.2f} {yoy:>10} {ma3:>10} {r['signal']:>+7}")
    print()


def main():
    p = argparse.ArgumentParser(description="Log monthly TSMC revenue (Signal B)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", action="store_true", help="Print the last 24 months")
    g.add_argument("--month", help="Reporting month, e.g. 2026-04")
    p.add_argument("--revenue", type=float,
                   help="Revenue in TWD billions (e.g. 285.7 for NT$285.7B)")
    args = p.parse_args()

    if args.show:
        cmd_show()
        return

    if args.revenue is None:
        p.error("--revenue is required when adding a month")

    result = add_tsmc_revenue(args.month, args.revenue)
    print(f"\n✅ Logged TSMC revenue for {args.month}")
    print(f"   Signal B (TSMC) is now: {result['signal']:+d}")


if __name__ == "__main__":
    main()
