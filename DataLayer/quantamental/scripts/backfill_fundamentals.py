"""
Backfill quarterly fundamentals for the candidate list (default).

Sources:
    yfinance (default) — Yahoo Finance, free, ~1-2 sec/ticker
    polygon            — Polygon.io, requires PAID plan (free tier returns 403)

Scope:
    By default, fundamentals are scoped to the **candidate list** (~26 tickers,
    ~30 seconds). This is the trading universe — fundamentals only matter for
    stocks you might actually trade. The full research universe (~1,386 tickers)
    is opt-in via --research-universe (~45 min, fragile under Yahoo rate limits).

Usage:
    python scripts/backfill_fundamentals.py                       # candidate list (default, ~30s)
    python scripts/backfill_fundamentals.py --tickers NVDA AMD    # specific tickers
    python scripts/backfill_fundamentals.py --research-universe   # full S&P 1500 (slow, fragile)
    python scripts/backfill_fundamentals.py --source polygon      # if you have paid Polygon

Free-tier timing (yfinance):
    - candidate list (~26 tickers): ~30 seconds
    - research universe (~1,386 tickers): ~45 minutes (rate-limit gymnastics)
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.universe import (
    load_candidate_list,
    load_research_universe,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("backfill_fundamentals")


def main():
    p = argparse.ArgumentParser(description="Backfill fundamentals into QuestDB")

    universe_group = p.add_mutually_exclusive_group()
    universe_group.add_argument(
        "--research-universe", action="store_true",
        help="Fetch fundamentals for the full S&P 1500 research universe "
             "(~1,386 tickers, ~45 min, fragile under Yahoo rate limits). "
             "Default is the candidate list only (~26 tickers, ~30s).")
    universe_group.add_argument(
        "--candidates-only", action="store_true",
        help="(default behaviour — kept for backwards compat) Fetch fundamentals "
             "for the candidate list only")
    universe_group.add_argument(
        "--tickers", nargs="+",
        help="Specific tickers to fetch (overrides universe)")

    p.add_argument(
        "--source",
        choices=["yfinance", "polygon"],
        default="yfinance",
        help="Data source (default: yfinance — Polygon requires paid plan)",
    )
    p.add_argument(
        "--income-only",
        action="store_true",
        help="(polygon source only) Skip balance sheet + cash flow — 3x faster",
    )
    p.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Re-fetch tickers that already have data in the DB",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch but do not write to QuestDB",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="(yfinance source) Politeness delay between calls in seconds (default: 1.0)",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="(yfinance source) Pause every N tickers to reset Yahoo rate-limit window (default: 100)",
    )
    p.add_argument(
        "--batch-pause",
        type=float,
        default=0.0,
        help="(yfinance source) Seconds to pause every --batch-size tickers. "
             "Default 0 (no pause — fine for candidate list). For "
             "--research-universe, use ~45 to avoid Yahoo IP-level throttling.",
    )

    args = p.parse_args()

    # Initialize schemas
    from data.ingest.questdb_writer import init_schema
    init_schema()

    # Determine universe — candidate list is the default (fundamentals are only
    # consumed by candidate-scoped signals; full universe is opt-in)
    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
        logger.info("Backfilling specific tickers: %s", tickers)
    elif args.research_universe:
        tickers = load_research_universe()
        logger.info("Backfilling research universe (%d tickers) — slow/fragile path",
                    len(tickers))
    else:
        # Default: candidate list (also reached via legacy --candidates-only flag)
        tickers = load_candidate_list()
        logger.info("Backfilling candidate list (%d tickers)", len(tickers))

    # Dispatch by source
    if args.source == "yfinance":
        from data.ingest.yfinance_fundamentals import backfill_fundamentals_yf

        # For full-universe runs, auto-enable a batch pause to dodge Yahoo throttling
        # unless the user explicitly set it via --batch-pause.
        effective_batch_pause = args.batch_pause
        if args.research_universe and effective_batch_pause == 0.0:
            effective_batch_pause = 45.0
            logger.info("--research-universe selected: auto-setting --batch-pause=45 "
                        "(override with --batch-pause N)")

        result = backfill_fundamentals_yf(
            tickers,
            skip_existing=not args.no_skip_existing,
            persist=not args.dry_run,
            delay_after=args.delay,
            batch_size=args.batch_size,
            batch_pause=effective_batch_pause,
        )
        print(f"\n✅ yfinance backfill complete: {result}")

    elif args.source == "polygon":
        if args.income_only:
            from data.ingest.polygon_fundamentals import fetch_income_statements
            from data.ingest.questdb_writer import write_fundamentals, query

            skip_set = set()
            if not args.no_skip_existing:
                try:
                    existing = query(
                        "SELECT symbol, count() AS n FROM fundamentals GROUP BY symbol"
                    )
                    skip_set = set(existing[existing["n"] >= 8]["symbol"])
                except Exception:
                    pass
            to_fetch = [t for t in tickers if t not in skip_set]
            logger.info("Polygon income-only: %d to fetch (%d skipped)",
                        len(to_fetch), len(skip_set))

            fetched = failed = 0
            for i, ticker in enumerate(to_fetch, 1):
                df = fetch_income_statements(ticker, limit=8)
                if df.empty:
                    failed += 1
                    continue
                if not args.dry_run:
                    write_fundamentals(df)
                fetched += 1
                if i % 50 == 0:
                    logger.info("[%d/%d] fetched=%d failed=%d", i, len(to_fetch), fetched, failed)
            logger.info("Done: fetched=%d failed=%d", fetched, failed)
        else:
            from data.ingest.polygon_fundamentals import backfill_fundamentals
            backfill_fundamentals(
                tickers,
                quarters=8,
                skip_existing=not args.no_skip_existing,
                persist=not args.dry_run,
            )


if __name__ == "__main__":
    main()
