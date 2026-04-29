"""
Historical data backfill script.

Strategy auto-picks based on universe size:
  - Small universe (≤ PER_TICKER_THRESHOLD)  → per-ticker (1 call per ticker, full range)
  - Large universe  (> threshold)             → per-date  (1 call per date, all tickers)

Per-date wins for ~1,200 ticker research universe:
    1,200 tickers × 1 call each  vs  ~250 dates × 1 call each
    On free tier (5/min): 1,200 × 12s = 4 hours  vs  250 × 12s = 50 minutes

Usage:
    python scripts/backfill.py                                        # default start 2024-06-01
    python scripts/backfill.py --start 2024-01-01                     # custom start
    python scripts/backfill.py --start 2024-01-01 --end 2024-12-31
    python scripts/backfill.py --skip-macro                           # OHLCV only
    python scripts/backfill.py --strategy per-ticker                  # force a strategy
    python scripts/backfill.py --candidates-only                      # backfill only the candidate list
    python scripts/backfill.py --tickers NVDA AMD MSFT                # specific tickers only
"""

import argparse
import logging
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.universe import (
    BASE_CANDIDATE_TICKERS,
    load_candidate_list,
    load_research_universe,
    research_universe_source,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("backfill")


# Default backfill window — ~10 months of history is enough for the 60-day MA
# plus enough buffer for the regime classifier to have stable scores.
DEFAULT_START = "2024-06-01"

# Crossover point — below this many tickers, per-ticker is faster on free tier
PER_TICKER_THRESHOLD = 100


# ── OHLCV strategies ──────────────────────────────────────────────────────────

def backfill_ohlcv_per_ticker(tickers: list[str], start: date, end: date):
    """Original strategy: 1 API call per ticker (returns full date range).

    Fast for small universes (< PER_TICKER_THRESHOLD).
    """
    from data.ingest import polygon_client, questdb_writer

    logger.info("Per-ticker strategy: %d tickers × 1 call each", len(tickers))

    # Skip rows already in QuestDB
    existing = _existing_pairs(start, end, questdb_writer)

    df = polygon_client.fetch_date_range(tickers, start, end)
    if df.empty:
        logger.warning("No OHLCV data returned")
        return

    new_rows = _filter_new(df, existing)
    if new_rows.empty:
        logger.info("All OHLCV already present — nothing to insert")
        return

    questdb_writer.write_ohlcv(new_rows)
    logger.info("OHLCV per-ticker backfill complete: %d new rows", len(new_rows))


def backfill_ohlcv_per_date(tickers: list[str], start: date, end: date):
    """Faster strategy for large universes: 1 grouped API call per date.

    Each call returns ALL US stocks for that date — we filter to our universe
    in memory. For 1,200 tickers across 250 trading days = 250 calls vs 1,200.

    Skips weekends AND NYSE market holidays (Juneteenth, Thanksgiving, etc.)
    using pandas_market_calendars when available — saves ~20 wasted API calls
    per year of backfill on the free tier.
    """
    from data.ingest import polygon_client, questdb_writer

    # Build set of valid NYSE trading days in the window. Falls back to
    # weekday-only filtering if pandas_market_calendars is unavailable.
    valid_trading_days = _nyse_trading_days(start, end)
    logger.info(
        "Per-date strategy: %d NYSE trading days × 1 call each (window: %s → %s)",
        len(valid_trading_days), start, end,
    )

    existing = _existing_pairs(start, end, questdb_writer)

    cur = start
    days_done = 0
    days_skipped = 0
    rows_inserted = 0
    universe_set = set(tickers)

    while cur <= end:
        # Skip non-trading days (weekends + NYSE holidays)
        if cur not in valid_trading_days:
            cur += timedelta(days=1)
            continue

        # Skip if this date is already fully populated
        already_for_date = {pair[0] for pair in existing if pair[1] == cur.isoformat()}
        if len(already_for_date & universe_set) >= len(universe_set) * 0.95:
            days_skipped += 1
            cur += timedelta(days=1)
            continue

        df = polygon_client.fetch_grouped_daily(target_date=cur, tickers=tickers)
        if not df.empty:
            new_rows = _filter_new(df, existing)
            if not new_rows.empty:
                questdb_writer.write_ohlcv(new_rows)
                rows_inserted += len(new_rows)

        days_done += 1
        cur += timedelta(days=1)

    logger.info(
        "OHLCV per-date backfill complete: %d days fetched, %d skipped (already complete), %d rows inserted",
        days_done, days_skipped, rows_inserted,
    )


def _existing_pairs(start: date, end: date, writer) -> set:
    try:
        existing_df = writer.query(
            f"""
            SELECT symbol, ts::date AS d
            FROM daily_ohlcv
            WHERE ts >= '{start}' AND ts <= '{end}'
            """
        )
        return set(zip(existing_df["symbol"], existing_df["d"].astype(str)))
    except Exception as exc:
        logger.warning("Could not query existing data (%s) — will insert all", exc)
        return set()


def _filter_new(df, existing: set):
    if df.empty:
        return df
    df = df.copy()
    df["date_str"] = df["ts"].dt.date.astype(str)
    mask = df.apply(lambda r: (r["symbol"], r["date_str"]) not in existing, axis=1)
    return df[mask].drop(columns=["date_str"])


def _count_trading_days(start: date, end: date) -> int:
    return sum(
        1 for n in range((end - start).days + 1)
        if (start + timedelta(days=n)).weekday() < 5
    )


def _nyse_trading_days(start: date, end: date) -> set[date]:
    """Return the set of NYSE trading days between start and end (inclusive).

    Uses pandas_market_calendars XNYS if available (skips holidays).
    Falls back to all weekdays if the package isn't installed.
    """
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("XNYS")
        valid = nyse.valid_days(start_date=start.isoformat(), end_date=end.isoformat())
        return {d.date() for d in valid}
    except Exception as exc:
        logger.warning(
            "pandas_market_calendars unavailable (%s) — backfill will hit API "
            "on holidays (~20/year wasted calls)", exc,
        )
        return {
            start + timedelta(days=n)
            for n in range((end - start).days + 1)
            if (start + timedelta(days=n)).weekday() < 5
        }


# ── Macro backfill (unchanged in spirit) ──────────────────────────────────────

def backfill_macro(start: date, end: date):
    from data.ingest import fred_client, questdb_writer
    from signals.macro import (
        score_credit_spread, score_fed_balance, score_vix, score_yield,
    )

    logger.info("Backfilling macro data: %s → %s", start, end)
    macro_data = fred_client.fetch_all_macro(start=start, end=end)

    signal_fns = {
        "yield_10y":     score_yield,
        "fed_balance":   score_fed_balance,
        "credit_spread": score_credit_spread,
    }

    for name, df in macro_data.items():
        if df.empty:
            logger.warning("No FRED data for %s", name)
            continue

        df = df.sort_values("ts").reset_index(drop=True)
        series = df["value"].dropna()

        df["ma_20"] = series.rolling(20).mean()
        df["ma_60"] = series.rolling(60).mean()

        if name == "vix":
            latest_vix = float(series.iloc[-1]) if not series.empty else 20.0
            df["signal"] = score_vix(latest_vix)
        elif name in signal_fns:
            df["signal"] = signal_fns[name](df)
        else:
            df["signal"] = 0

        questdb_writer.write_macro(df, name)
        logger.info("Wrote %d rows for macro indicator: %s", len(df), name)


# ── Orchestration ─────────────────────────────────────────────────────────────

def pick_strategy(n_tickers: int, override: str | None) -> str:
    if override:
        return override
    return "per-date" if n_tickers > PER_TICKER_THRESHOLD else "per-ticker"


def main():
    parser = argparse.ArgumentParser(description="Backfill historical data into QuestDB")
    parser.add_argument("--start", default=DEFAULT_START,
                        help=f"Start date YYYY-MM-DD (default: {DEFAULT_START})")
    parser.add_argument("--end", default=date.today().isoformat(),
                        help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--skip-ohlcv", action="store_true", help="Skip OHLCV backfill")
    parser.add_argument("--skip-macro", action="store_true", help="Skip macro backfill")
    parser.add_argument(
        "--strategy",
        choices=["per-ticker", "per-date", "auto"],
        default="auto",
        help="Backfill strategy (default: auto-pick by universe size)",
    )
    parser.add_argument(
        "--candidates-only",
        action="store_true",
        help="Backfill only the candidate list (small universe), not the research universe",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        metavar="TICKER",
        help="Backfill specific tickers only (overrides --candidates-only and research universe)",
    )
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end)

    # Initialise schemas
    from data.ingest.questdb_writer import init_schema
    from portfolio.tracker import init_db
    init_schema()
    init_db()

    if not args.skip_ohlcv:
        if args.tickers:
            tickers = [t.upper() for t in args.tickers]
            logger.info("Backfilling SPECIFIC tickers (%d): %s", len(tickers), tickers)
        elif args.candidates_only:
            tickers = load_candidate_list()
            logger.info("Backfilling CANDIDATE list (%d tickers)", len(tickers))
        else:
            tickers = load_research_universe()
            logger.info("Backfilling RESEARCH universe (%d tickers, source: %s)",
                        len(tickers), research_universe_source())

        strategy = pick_strategy(len(tickers), args.strategy if args.strategy != "auto" else None)
        logger.info("Strategy: %s (universe=%d, threshold=%d)",
                    strategy, len(tickers), PER_TICKER_THRESHOLD)

        if strategy == "per-ticker":
            backfill_ohlcv_per_ticker(tickers, start, end)
        else:
            backfill_ohlcv_per_date(tickers, start, end)

    if not args.skip_macro:
        backfill_macro(start, end)

    logger.info("Backfill complete")


if __name__ == "__main__":
    main()
