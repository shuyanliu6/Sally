"""
Daily pipeline orchestrator with per-step retry and resume support.

Usage:
    python scripts/daily_pipeline.py --step all         # run all, skip today's completed steps
    python scripts/daily_pipeline.py --resume           # same as --step all (explicit alias)
    python scripts/daily_pipeline.py --step all --force # ignore state, re-run everything
    python scripts/daily_pipeline.py --step fetch_market  # always run this one step
    python scripts/daily_pipeline.py --step all --max-retries 5 --retry-delay 10
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

# Allow running from repo root or scripts/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import PIPELINE_LOG_PATH, SQLITE_PATH
from config.universe import (
    load_candidate_list,
    load_research_universe,
    research_universe_source,
)

# Ensure logs directory exists before setting up file handler
Path(PIPELINE_LOG_PATH).parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(PIPELINE_LOG_PATH),
    ],
)
logger = logging.getLogger("pipeline")


# ── State file (resume support) ────────────────────────────────────────────────

def _state_path() -> Path:
    return Path(PIPELINE_LOG_PATH).parent / f"pipeline_state_{date.today()}.json"


def _load_state(force: bool = False) -> dict:
    """Load today's run state. Returns empty state if missing or --force."""
    path = _state_path()
    if force or not path.exists():
        return {"completed": [], "failed": []}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"completed": [], "failed": []}


def _save_state(state: dict):
    try:
        _state_path().write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.warning("Could not save pipeline state: %s", e)


# ── Retry helper ───────────────────────────────────────────────────────────────

def with_retry(fn, step_name: str, max_retries: int = 3, delay: int = 5):
    """Call fn() with exponential backoff retry. Raises on final failure."""
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == max_retries:
                logger.error(
                    "Step %s failed permanently after %d attempts: %s",
                    step_name, max_retries, exc, exc_info=True,
                )
                raise
            wait = delay * attempt   # 5s → 10s → 15s …
            logger.warning(
                "Step %s attempt %d/%d failed: %s — retrying in %ds",
                step_name, attempt, max_retries, exc, wait,
            )
            time.sleep(wait)


# ── Step functions ─────────────────────────────────────────────────────────────

def step_fetch_market() -> bool:
    from data.ingest import polygon_client, questdb_writer
    logger.info("STEP: fetch_market")
    target = polygon_client.prev_trading_day()

    # Use research universe if available, else fall back to candidate list.
    # Both are loaded dynamically — system adapts when user updates the JSON files.
    universe = load_research_universe()
    logger.info(
        "Fetching EOD data for %s (1 grouped API call, %d tickers, source: %s)",
        target, len(universe), research_universe_source(),
    )

    df = polygon_client.fetch_grouped_daily(target_date=target, tickers=universe)
    if df.empty:
        logger.warning("No OHLCV data returned for %s", target)
        return False
    df = polygon_client.validate_ohlcv(df)
    questdb_writer.write_ohlcv(df)
    logger.info("fetch_market complete: %d rows", len(df))
    return True


def step_fetch_macro() -> bool:
    from data.ingest import fred_client, questdb_writer
    from signals.macro import score_credit_spread, score_fed_balance, score_vix, score_yield
    from datetime import timedelta

    logger.info("STEP: fetch_macro")
    # D1 efficiency: only pull the last 90 days from FRED — write_macro
    # deduplicates by ts, so older data is wasted bandwidth. 90 days gives
    # plenty of headroom for the 60-day MA used by signals.
    fetch_start = date.today() - timedelta(days=90)
    macro_data = fred_client.fetch_all_macro(start=fetch_start)

    signal_fns = {
        "yield_10y":     score_yield,
        "fed_balance":   score_fed_balance,
        "credit_spread": score_credit_spread,
    }

    for name, df in macro_data.items():
        if df.empty:
            logger.warning("Empty data for %s, skipping", name)
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

    logger.info("fetch_macro complete")
    return True


def step_calc_signals() -> bool:
    from data.ingest import fred_client, questdb_writer
    from signals.aggregator import run_and_store
    from datetime import timedelta

    logger.info("STEP: calc_signals")
    # Need 60-day MA for yield + credit spread, 13-week MA for Fed BS — pull
    # 1 year to give the moving averages enough history.
    signal_start = date.today() - timedelta(days=365)
    macro_data = fred_client.fetch_all_macro(start=signal_start)

    row = run_and_store(
        yield_df=macro_data.get("yield_10y", _empty_df()),
        vix_df=macro_data.get("vix", _empty_df()),
        fed_df=macro_data.get("fed_balance", _empty_df()),
        credit_df=macro_data.get("credit_spread", _empty_df()),
        writer=questdb_writer,
    )
    logger.info("calc_signals complete: regime=%s score=%d", row["regime"], row["composite_score"])
    return True


def step_calc_sector_signals() -> bool:
    """Compute Signal A (SOX/SPX) + signals B/C/D (TSMC/Capex/API pricing)
    and persist a row to sector_signals.

    Signals B/C/D read from SQLite (manual entry via log_*.py CLIs); they
    return 0 when no data is logged yet, so this step is safe to run before
    any manual data has been entered.
    """
    from signals.sector import run_sector_signals

    logger.info("STEP: calc_sector_signals")
    row = run_sector_signals(persist=True)
    logger.info(
        "calc_sector_signals complete: composite=%+d (sox/spx=%+d tsmc=%+d capex=%+d api=%+d)",
        row["sector_composite"], row["sox_spx_signal"],
        row["tsmc_signal"], row["capex_signal"], row["api_pricing_signal"],
    )
    return True


def step_calc_stock_signals() -> bool:
    """Compute EMA(20/60) / RSI(14) / Volume / PEAD signals for the candidate list
    and persist one row per ticker to stock_signals.

    Scoped to candidate list only (~56 tickers). Tickers without enough OHLCV
    history (need ≥60 days for the slow EMA) score neutral. Cross events
    (golden/death) are written to signal_events automatically.
    """
    from signals.stock import compute_stock_signals_for_universe

    logger.info("STEP: calc_stock_signals")
    candidates = load_candidate_list()
    df = compute_stock_signals_for_universe(universe=candidates, persist=True)
    logger.info("calc_stock_signals complete: %d/%d tickers scored",
                len(df), len(candidates))
    return True


def step_update_portfolio() -> bool:
    from data.ingest.questdb_writer import query
    from portfolio.tracker import compute_pnl, get_open_positions

    logger.info("STEP: update_portfolio")
    positions = get_open_positions(SQLITE_PATH)
    if positions.empty:
        logger.info("No open positions to update")
        return True

    prices_df = query("SELECT symbol, close FROM daily_ohlcv LATEST ON ts PARTITION BY symbol")
    latest_prices = dict(zip(prices_df["symbol"], prices_df["close"]))

    pnl_df = compute_pnl(positions, latest_prices)
    logger.info(
        "Portfolio updated: %d positions, total P&L=%.2f",
        len(pnl_df),
        pnl_df["pnl"].sum() if "pnl" in pnl_df else 0,
    )
    return True


def step_check_stops() -> bool:
    from data.ingest.questdb_writer import query
    from portfolio.stoploss import check_stops, format_stop_alerts, send_telegram_alert
    from portfolio.tracker import get_open_positions

    logger.info("STEP: check_stops")
    positions = get_open_positions(SQLITE_PATH)
    if positions.empty:
        logger.info("No open positions to check")
        return True

    prices_df = query("SELECT symbol, close FROM daily_ohlcv LATEST ON ts PARTITION BY symbol")
    latest_prices = dict(zip(prices_df["symbol"], prices_df["close"]))

    alerts = check_stops(positions, latest_prices)
    if alerts:
        message = format_stop_alerts(alerts)
        logger.warning(message)
        send_telegram_alert(message)
    else:
        logger.info("No stop-loss alerts")
    return True


def step_refresh_fundamentals() -> bool:
    """Refresh quarterly fundamentals for the candidate list.

    Cadence: Mondays only (skipped silently on other days). Cheap (~30s) so it
    can ride along with the daily pipeline without slowing it down materially.

    Behaviour:
        - On Monday: refresh candidate-list fundamentals via yfinance,
          re-fetching everything (skip_existing=False) so revisions
          propagate. Returns True regardless of per-ticker outcomes —
          we don't want a single 404 to fail the whole pipeline.
        - Other days: log + return True without doing any work.
    """
    logger.info("STEP: refresh_fundamentals")
    if datetime.today().weekday() != 0:  # 0 = Monday
        logger.info("Not Monday — skipping fundamentals refresh "
                    "(weekly cadence keeps Yahoo happy)")
        return True

    from data.ingest.yfinance_fundamentals import backfill_fundamentals_yf

    candidates = load_candidate_list()
    logger.info("Refreshing fundamentals for %d candidates", len(candidates))
    summary = backfill_fundamentals_yf(
        candidates,
        skip_existing=False,   # always re-fetch on the weekly cadence
        persist=True,
        delay_after=1.0,
        batch_pause=0.0,       # candidate list is small enough to skip batching
    )
    logger.info("refresh_fundamentals summary: %s", summary)
    return True


def _empty_df():
    import pandas as pd
    return pd.DataFrame(columns=["ts", "value"])


# ── Orchestration ──────────────────────────────────────────────────────────────

STEPS = {
    "fetch_market":          step_fetch_market,
    "fetch_macro":           step_fetch_macro,
    "calc_signals":          step_calc_signals,
    "calc_sector_signals":   step_calc_sector_signals,
    "calc_stock_signals":    step_calc_stock_signals,
    "refresh_fundamentals":  step_refresh_fundamentals,
    "update_portfolio":      step_update_portfolio,
    "check_stops":           step_check_stops,
}

ALL_STEPS = [
    "fetch_market",
    "fetch_macro",
    "calc_signals",
    "calc_sector_signals",
    "calc_stock_signals",
    "refresh_fundamentals",   # Monday-only; no-op other days
    "update_portfolio",
    "check_stops",
]


def run_pipeline(
    step: str,
    force: bool = False,
    max_retries: int = 3,
    retry_delay: int = 5,
):
    from data.ingest.questdb_writer import init_schema
    from portfolio.tracker import init_db

    logger.info("=== Pipeline started: step=%s force=%s at %s ===",
                step, force, datetime.now(UTC).isoformat())

    # Ensure schema exists
    try:
        init_schema()
        init_db()
    except Exception as e:
        logger.warning("Schema init warning: %s", e)

    # Determine which steps to run
    if step in ("all", "resume"):
        steps_to_run = ALL_STEPS
        state = _load_state(force=force)
        already_done = set(state["completed"])
    else:
        # Single explicit step — always run it, no state tracking
        steps_to_run = [step]
        state = None
        already_done = set()

    results = {}

    for s in steps_to_run:
        if s in already_done:
            logger.info("SKIP %s (already completed today — use --force to re-run)", s)
            results[s] = "SKIP"
            continue

        try:
            with_retry(STEPS[s], step_name=s, max_retries=max_retries, delay=retry_delay)
            results[s] = "OK"
            if state is not None:
                state["completed"].append(s)
                state["failed"] = [f for f in state["failed"] if f != s]
                _save_state(state)
        except Exception:
            results[s] = "FAIL"
            if state is not None:
                if s not in state["failed"]:
                    state["failed"].append(s)
                _save_state(state)

    # Summary
    lines = ["=== Pipeline summary ==="]
    icons = {"OK": "✅", "SKIP": "⏭ ", "FAIL": "❌", "WARN": "⚠️ "}
    for s, r in results.items():
        lines.append(f"  {icons.get(r, '?')} {s}: {r}")
    if state and state["failed"]:
        lines.append(f"\nFailed steps: {state['failed']}")
        lines.append("Re-run with:  python scripts/daily_pipeline.py --resume")
    logger.info("\n".join(lines))

    failed = [s for s, r in results.items() if r == "FAIL"]
    return len(failed) == 0


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily quantamental pipeline")

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--step",
        choices=list(STEPS.keys()) + ["all"],
        default="all",
        help="Which step(s) to run. 'all' skips today's completed steps (default: all)",
    )
    group.add_argument(
        "--resume",
        action="store_true",
        help="Alias for --step all: skip completed steps, retry failed ones",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore today's state file and re-run all steps from scratch",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        metavar="N",
        help="Max retry attempts per step (default: 3)",
    )
    parser.add_argument(
        "--retry-delay",
        type=int,
        default=5,
        metavar="SECS",
        help="Base delay in seconds between retries, multiplied per attempt (default: 5)",
    )

    args = parser.parse_args()
    step_arg = "all" if args.resume else args.step

    success = run_pipeline(
        step=step_arg,
        force=args.force,
        max_retries=args.max_retries,
        retry_delay=args.retry_delay,
    )
    sys.exit(0 if success else 1)
