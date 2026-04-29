"""
Polygon Financials API client (Month 2, fundamentals layer).

Pulls quarterly income/balance/cash-flow data per ticker via Polygon's
`/v3/reference/financials` endpoints. Most important for:
  - PEAD signal (Stock Signal §4.4): needs reported EPS + report date
  - Future valuation signals: P/E, FCF yield, etc.

Polygon free tier: same 5 req/min rate limit as the OHLCV client. With 3
endpoints × 1,400 tickers = 4,200 calls × 12s ≈ 14 hours. To make this
manageable we:
  - default to `list_financials_income_statements` only (most useful)
  - support `tickers_any_of` batching when available
  - allow per-ticker incremental updates (skip already-stored periods)
"""

import logging
import time
from datetime import date, datetime
from typing import Iterator

import pandas as pd
from polygon import RESTClient

from config.settings import (
    POLYGON_API_KEY,
    POLYGON_RATE_LIMIT_BACKOFF,
)
from data.ingest.polygon_client import _is_rate_limit_error, _limiter

logger = logging.getLogger(__name__)


_client: RESTClient | None = None


def _get_client() -> RESTClient:
    global _client
    if _client is None:
        _client = RESTClient(api_key=POLYGON_API_KEY)
    return _client


def _safe_attr(obj, *path, default=None):
    """Walk nested attributes safely. Polygon returns deeply-nested objects."""
    for p in path:
        if obj is None:
            return default
        obj = getattr(obj, p, None)
    return obj if obj is not None else default


def _flatten_income_statement(stmt) -> dict:
    """Polygon's income statement model returns a dict-like with `.value` per metric.
    Pull out the canonical fields we care about, returning None for missing values.
    """
    return {
        "revenue":     _safe_attr(stmt, "revenues", "value"),
        "net_income":  _safe_attr(stmt, "net_income_loss", "value"),
        "eps_basic":   _safe_attr(stmt, "basic_earnings_per_share", "value"),
        "eps_diluted": _safe_attr(stmt, "diluted_earnings_per_share", "value"),
    }


def _flatten_balance_sheet(stmt) -> dict:
    return {
        "total_assets":         _safe_attr(stmt, "assets", "value"),
        "total_debt":           _safe_attr(stmt, "long_term_debt", "value"),
        "shares_outstanding":   _safe_attr(stmt, "common_stock_shares_outstanding", "value"),
    }


def _flatten_cash_flow(stmt) -> dict:
    return {
        "operating_cash_flow": _safe_attr(stmt, "net_cash_flow_from_operating_activities", "value"),
        "free_cash_flow":      _safe_attr(stmt, "free_cash_flow", "value"),
    }


def fetch_income_statements(
    ticker: str,
    limit: int = 8,
    retries: int = 3,
) -> pd.DataFrame:
    """Fetch the most recent N quarterly income statements for one ticker.

    Returns DataFrame with columns: symbol, period_end, fiscal_period,
    revenue, net_income, eps_basic, eps_diluted.
    Empty DataFrame on failure.
    """
    client = _get_client()
    records = []

    for attempt in range(1, retries + 1):
        try:
            _limiter.wait()
            stmts = list(client.list_financials_income_statements(
                tickers=ticker,
                timeframe="quarterly",
                limit=limit,
            ))
            for s in stmts:
                period_end = _safe_attr(s, "period_end_date") or _safe_attr(s, "filing_date")
                if not period_end:
                    continue
                fiscal_q = _safe_attr(s, "fiscal_quarter")
                fiscal_period = f"Q{int(fiscal_q)}" if fiscal_q else None

                rec = {
                    "symbol":        ticker,
                    "period_end":    pd.Timestamp(period_end, tz="UTC"),
                    "fiscal_period": fiscal_period,
                    **_flatten_income_statement(s),
                }
                records.append(rec)
            return pd.DataFrame(records)

        except Exception as exc:
            if _is_rate_limit_error(exc):
                wait = POLYGON_RATE_LIMIT_BACKOFF
                logger.warning("fundamentals %s: 429 — waiting %ds (attempt %d/%d)",
                               ticker, wait, attempt, retries)
                time.sleep(wait)
            else:
                wait = 5 * attempt
                logger.warning("fundamentals %s attempt %d/%d failed: %s — retrying in %ds",
                               ticker, attempt, retries, exc, wait)
                time.sleep(wait)

    logger.error("fundamentals %s: gave up after %d attempts", ticker, retries)
    return pd.DataFrame()


def fetch_full_fundamentals(
    ticker: str,
    limit: int = 8,
    include_balance: bool = True,
    include_cash_flow: bool = True,
) -> pd.DataFrame:
    """Fetch income + (optional) balance sheet + (optional) cash flow for a ticker.

    Returns one row per fiscal period with merged columns from all three statements.
    Each statement type is a separate API call (3x rate-limit cost when all enabled).
    """
    income = fetch_income_statements(ticker, limit=limit)
    if income.empty:
        return income

    if not (include_balance or include_cash_flow):
        return income

    client = _get_client()
    extras = {}  # period_end -> dict

    for fetcher_name, flatten_fn, enabled in [
        ("list_financials_balance_sheets", _flatten_balance_sheet, include_balance),
        ("list_financials_cash_flow_statements", _flatten_cash_flow, include_cash_flow),
    ]:
        if not enabled:
            continue
        try:
            _limiter.wait()
            stmts = list(getattr(client, fetcher_name)(
                tickers=ticker, timeframe="quarterly", limit=limit,
            ))
            for s in stmts:
                period_end = _safe_attr(s, "period_end_date") or _safe_attr(s, "filing_date")
                if not period_end:
                    continue
                ts = pd.Timestamp(period_end, tz="UTC")
                extras.setdefault(ts, {}).update(flatten_fn(s))
        except Exception as exc:
            logger.warning("fundamentals %s: %s failed (%s) — continuing without",
                           ticker, fetcher_name, exc)

    # Merge into income DataFrame
    for col in ("total_assets", "total_debt", "shares_outstanding",
                "operating_cash_flow", "free_cash_flow"):
        income[col] = income["period_end"].map(lambda ts: extras.get(ts, {}).get(col))

    return income


def backfill_fundamentals(
    tickers: list[str],
    quarters: int = 8,
    skip_existing: bool = True,
    persist: bool = True,
) -> dict:
    """Backfill quarterly fundamentals for a list of tickers.

    Args:
        tickers: ticker symbols to fetch
        quarters: how many recent quarters (default 8 = 2 years)
        skip_existing: skip tickers that already have ≥ `quarters` rows in DB
        persist: write to QuestDB (False for dry-run)

    Returns: dict with summary stats.
    """
    from data.ingest.questdb_writer import query, write_fundamentals

    # Find which tickers already have data
    skip_tickers: set[str] = set()
    if skip_existing:
        try:
            existing = query(
                "SELECT symbol, count() AS n FROM fundamentals GROUP BY symbol"
            )
            skip_tickers = set(existing[existing["n"] >= quarters]["symbol"])
        except Exception:
            skip_tickers = set()

    to_fetch = [t for t in tickers if t not in skip_tickers]
    logger.info("Backfilling fundamentals for %d tickers (skipping %d already complete)",
                len(to_fetch), len(skip_tickers))

    fetched = 0
    failed = 0
    for i, ticker in enumerate(to_fetch, 1):
        df = fetch_full_fundamentals(ticker, limit=quarters)
        if df.empty:
            failed += 1
            continue
        if persist:
            write_fundamentals(df)
        fetched += 1
        if i % 50 == 0:
            logger.info("[fundamentals] %d/%d tickers done (%d failed)",
                        i, len(to_fetch), failed)

    summary = {
        "requested": len(tickers),
        "skipped":   len(skip_tickers),
        "fetched":   fetched,
        "failed":    failed,
    }
    logger.info("Fundamentals backfill complete: %s", summary)
    return summary
