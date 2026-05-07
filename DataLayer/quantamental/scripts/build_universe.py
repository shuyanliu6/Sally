"""
Build / refine the S&P 1500 research universe.

Stages:
    static  — scrape Wikipedia + Polygon types, apply REIT/ADR/SPAC filters
    refine  — filter by liquidity (price ≥ $5, ADDV ≥ $2M, age ≥ 252 days)
    all     — run static then refine

Schema migration (separate flag, destructive):
    --apply-schema-migration  → drop & recreate daily_ohlcv with bumped SYMBOL CAPACITY
                                (needed once when going from 27 → ~1,200 universe)

Usage:
    python scripts/build_universe.py --stage static
    python scripts/build_universe.py --stage refine
    python scripts/build_universe.py --stage all
    python scripts/build_universe.py --apply-schema-migration
"""

import argparse
import logging
import os
import sys

if __package__ in (None, ""):
    from _bootstrap import add_project_root
    add_project_root(__file__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("build_universe")


def cmd_static():
    from quantamental.research.universe_builder import build_static_universe
    payload = build_static_universe()
    print(f"\n✅ Static universe written: {payload['ticker_count']} tickers")
    print(f"   Drops: {payload['stats']['drops']}")


def cmd_refine(price_floor: float, addv_min: float, min_history: int):
    from quantamental.research.universe_builder import refine_universe_with_liquidity
    payload = refine_universe_with_liquidity(
        price_floor=price_floor,
        addv_min=addv_min,
        min_history_days=min_history,
    )
    print(f"\n✅ Refined universe written: {payload['ticker_count']} tickers")
    print(f"   Liquidity drops: {payload['liquidity_stats'].get('drops', {})}")


def cmd_apply_schema_migration():
    from quantamental.data.ingest.questdb_writer import recreate_ohlcv_table

    print("\n⚠️  This will DROP the daily_ohlcv table and all its data.")
    print("   You will need to backfill afterwards: python scripts/backfill.py")
    answer = input("   Proceed? [y/N] ")
    if answer.strip().lower() != "y":
        print("Aborted.")
        return
    recreate_ohlcv_table()
    print("\n✅ daily_ohlcv recreated with SYMBOL CAPACITY 2048")
    print("   Next: python scripts/backfill.py  (default start: 2024-06-01)")


def main():
    p = argparse.ArgumentParser(description="Build the research universe")
    p.add_argument(
        "--stage",
        choices=["static", "refine", "all"],
        help="Which stage(s) to run",
    )
    p.add_argument(
        "--apply-schema-migration",
        action="store_true",
        help="Drop & recreate daily_ohlcv with new SYMBOL CAPACITY (destructive!)",
    )
    p.add_argument("--price-floor", type=float, default=5.0,
                   help="Minimum latest close price for refine stage (default: $5)")
    p.add_argument("--addv-min", type=float, default=2_000_000.0,
                   help="Minimum trailing-30d ADDV in USD for refine stage (default: $2M)")
    p.add_argument("--min-history", type=int, default=252,
                   help="Minimum trading days of history (default: 252)")

    args = p.parse_args()

    if args.apply_schema_migration:
        cmd_apply_schema_migration()
        return

    if not args.stage:
        p.error("Either --stage or --apply-schema-migration is required")

    if args.stage in ("static", "all"):
        cmd_static()
    if args.stage in ("refine", "all"):
        cmd_refine(args.price_floor, args.addv_min, args.min_history)


if __name__ == "__main__":
    main()
