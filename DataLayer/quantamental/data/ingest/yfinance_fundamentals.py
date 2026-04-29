"""
yfinance-based fundamentals fallback (Month 2).

Polygon's free tier doesn't include `/v3/reference/financials`. yfinance
(Yahoo Finance) is the fastest free alternative — no API key, decent quality,
covers our universe.

Tradeoffs vs Polygon:
    + free, no auth
    + bulk fetch is fast (~1 sec/ticker, no hard rate limit but be polite)
    + has all fields we need (revenue, EPS, total assets/debt, OCF/FCF, shares)
    - occasional 404s for newly-listed or delisted tickers
    - field names sometimes shift between yfinance versions
    - Yahoo Finance can rate-limit aggressive scrapers

Use a small per-call delay to be a good citizen. Field-name lookups are tolerant:
multiple aliases per metric, fall back to None when not present.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Iterable

import pandas as pd

logger = logging.getLogger(__name__)


# ── Shared curl_cffi session (Chrome impersonation) ───────────────────────────
# Created once at import time so we reuse the TLS connection pool.
# Falls back to None (yfinance uses its default requests session) if curl_cffi
# is not installed.
_YF_SESSION = None
try:
    from curl_cffi import requests as _cffi_requests
    _YF_SESSION = _cffi_requests.Session(impersonate="chrome")
    logger.debug("curl_cffi Chrome session initialised for yfinance")
except ImportError:
    logger.debug("curl_cffi not available — yfinance will use its default session")


# ── Field-name aliases ────────────────────────────────────────────────────────
# yfinance field names occasionally change between versions. Try each alias
# in order until one is found in the row index.
INCOME_FIELD_MAP = {
    "revenue":     ["Total Revenue", "Revenues", "Operating Revenue"],
    "net_income":  ["Net Income", "Net Income Common Stockholders",
                    "Net Income From Continuing Operation Net Minority Interest"],
    "eps_basic":   ["Basic EPS", "Basic Earnings Per Share"],
    "eps_diluted": ["Diluted EPS", "Diluted Earnings Per Share"],
}

BALANCE_FIELD_MAP = {
    "total_assets":       ["Total Assets"],
    "total_debt":         ["Total Debt", "Long Term Debt"],
    "shares_outstanding": ["Ordinary Shares Number", "Share Issued",
                           "Common Stock Equity"],
}

CASHFLOW_FIELD_MAP = {
    "operating_cash_flow": ["Operating Cash Flow",
                            "Cash Flow From Continuing Operating Activities"],
    "free_cash_flow":      ["Free Cash Flow"],
}

# Per-call politeness delay (seconds). Yahoo's unofficial rate is generous
# but bursts of 1000s of calls trigger temporary blocks.
DEFAULT_DELAY = 0.3


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_lookup(df: pd.DataFrame, aliases: list[str], col):
    """Return the first matching field's value at column `col`, or None."""
    if df is None or df.empty:
        return None
    for name in aliases:
        if name in df.index:
            try:
                val = df.loc[name, col]
                if pd.notna(val):
                    return float(val)
            except (KeyError, ValueError, TypeError):
                continue
    return None


def _fiscal_period_from_date(d: pd.Timestamp) -> str:
    """Approximate fiscal quarter label from a calendar period_end date.

    Yahoo Finance reports calendar-aligned quarters, so we just bucket by month.
    Companies whose fiscal year doesn't align with calendar (e.g., NVDA's Jan-end FY)
    will have a slight mismatch — acceptable for our PEAD use.
    """
    month = d.month
    if month <= 3:   return "Q1"
    if month <= 6:   return "Q2"
    if month <= 9:   return "Q3"
    return "Q4"


# ── Public API ────────────────────────────────────────────────────────────────

def _with_timeout(fn, timeout_sec: float, *args, **kwargs):
    """Run fn(*args, **kwargs) with a hard timeout. Returns None on timeout.

    Uses concurrent.futures so it works on any platform (signal-based timeouts
    don't work on threads / non-main threads). Cost: thread spawn per call,
    negligible vs the 1-2s fetch.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout_sec)
        except FutTimeout:
            return None


def fetch_fundamentals_yf(
    ticker: str,
    delay_after: float = DEFAULT_DELAY,
    timeout_sec: float = 30.0,
    max_retries: int = 3,
) -> pd.DataFrame:
    """Fetch quarterly fundamentals for one ticker via yfinance.

    Retries on:
      - Empty income statement (Yahoo intermittently rate-limits with empty bodies)
      - Timeout (default 15s per attempt)
      - Any exception inside the yfinance call
    Backs off 2s × attempt between retries.

    Returns DataFrame matching the QuestDB `fundamentals` schema.
    Returns empty DataFrame after exhausting retries.
    """
    def _fetch():
        import yfinance as yf
        # Reuse the module-level curl_cffi Chrome session (connection pool reuse,
        # avoids per-ticker TLS handshake overhead and looks more like a browser).
        t = yf.Ticker(ticker, session=_YF_SESSION) if _YF_SESSION is not None else yf.Ticker(ticker)
        return (t.quarterly_income_stmt,
                t.quarterly_balance_sheet,
                t.quarterly_cashflow)

    income = balance = cashflow = None
    last_failure_reason = None

    for attempt in range(1, max_retries + 1):
        if attempt == 1:
            logger.info("yfinance fetching %s ...", ticker)
        else:
            # Longer backoff (5s, 15s) — short waits don't clear a real throttle
            backoff = 5 * (3 ** (attempt - 2))  # 5, 15
            logger.warning("yfinance %s: retrying (attempt %d/%d) after %ds backoff — last failure: %s",
                           ticker, attempt, max_retries, backoff, last_failure_reason)
            time.sleep(backoff)

        try:
            result = _with_timeout(_fetch, timeout_sec)
        except Exception as exc:
            last_failure_reason = f"exception: {exc}"
            continue

        if result is None:
            last_failure_reason = f"timeout after {timeout_sec:.0f}s"
            continue

        income, balance, cashflow = result
        if income is None or income.empty:
            last_failure_reason = "empty income statement (likely Yahoo throttling)"
            continue

        # Got real data — exit retry loop
        break
    else:
        # All retries exhausted
        logger.warning("yfinance %s: gave up after %d attempts — %s",
                       ticker, max_retries, last_failure_reason)
        if delay_after:
            time.sleep(delay_after)
        return pd.DataFrame()

    if delay_after:
        time.sleep(delay_after)

    # Income statement columns are the period_end dates (most recent first)
    rows = []
    for col in income.columns:
        period_end = pd.Timestamp(col, tz="UTC")
        rec = {
            "symbol":        ticker,
            "period_end":    period_end,
            "fiscal_period": _fiscal_period_from_date(period_end),
        }

        # Income fields
        for canonical, aliases in INCOME_FIELD_MAP.items():
            rec[canonical] = _safe_lookup(income, aliases, col)

        # Balance sheet fields (may be on different date columns)
        for canonical, aliases in BALANCE_FIELD_MAP.items():
            rec[canonical] = _safe_lookup(balance, aliases, col) if balance is not None else None

        # Cash flow fields
        for canonical, aliases in CASHFLOW_FIELD_MAP.items():
            rec[canonical] = _safe_lookup(cashflow, aliases, col) if cashflow is not None else None

        rows.append(rec)

    df = pd.DataFrame(rows)

    # Cast shares_outstanding to int when present (matches QuestDB LONG)
    if "shares_outstanding" in df.columns:
        df["shares_outstanding"] = df["shares_outstanding"].apply(
            lambda x: int(x) if pd.notna(x) else None
        )

    logger.info("yfinance %s: %d quarters fetched", ticker, len(df))
    return df


def backfill_fundamentals_yf(
    tickers: Iterable[str],
    skip_existing: bool = True,
    persist: bool = True,
    quarters_threshold: int = 4,
    delay_after: float = DEFAULT_DELAY,
    batch_size: int = 100,
    batch_pause: float = 0.0,
) -> dict:
    """Backfill fundamentals for many tickers via yfinance.

    Defaults are tuned for the candidate-list workflow (~26 tickers, ~30s) where
    Yahoo's rate limiter never kicks in. For full-universe runs (~1,386 tickers)
    pass `batch_pause=45` from the CLI to avoid IP-level throttling.

    Args:
        tickers: ticker symbols to fetch
        skip_existing: skip tickers that already have ≥ quarters_threshold rows
        persist: write to QuestDB (False for dry-run)
        quarters_threshold: minimum rows in DB to consider a ticker "complete"
        delay_after: politeness delay between individual calls (default 0.3s)
        batch_size: pause every N tickers (only meaningful when batch_pause > 0)
        batch_pause: seconds to sleep between batches (default 0 — no pause)

    Returns: summary dict.
    """
    from data.ingest.questdb_writer import query, write_fundamentals

    tickers = [t.upper() for t in tickers]
    skip_set: set[str] = set()
    if skip_existing:
        try:
            existing = query(
                "SELECT symbol, count() AS n FROM fundamentals GROUP BY symbol"
            )
            skip_set = set(existing[existing["n"] >= quarters_threshold]["symbol"])
        except Exception:
            skip_set = set()

    to_fetch = [t for t in tickers if t not in skip_set]
    logger.info("yfinance backfill: %d to fetch (%d skipped, already have >=%d quarters)",
                len(to_fetch), len(skip_set), quarters_threshold)
    if batch_pause > 0:
        logger.info("Rate-limit strategy: %.1fs delay per call, %.0fs pause every %d tickers",
                    delay_after, batch_pause, batch_size)
    else:
        logger.info("Rate-limit strategy: %.1fs delay per call (no batch pause)", delay_after)

    fetched = failed = 0
    total_rows = 0

    for i, ticker in enumerate(to_fetch, 1):
        # Optional batch pause — only relevant for full-universe runs
        if batch_pause > 0 and i > 1 and (i - 1) % batch_size == 0:
            logger.info("[yf-fundamentals] batch pause %.0fs after %d tickers ...",
                        batch_pause, i - 1)
            time.sleep(batch_pause)

        df = fetch_fundamentals_yf(ticker, delay_after=delay_after)
        if df.empty:
            failed += 1
            continue

        if persist:
            write_fundamentals(df)
        fetched += 1
        total_rows += len(df)

        if i % 25 == 0 or i == len(to_fetch):
            logger.info("[yf-fundamentals] %d/%d done — fetched=%d failed=%d rows=%d",
                        i, len(to_fetch), fetched, failed, total_rows)

    summary = {
        "requested":  len(tickers),
        "skipped":    len(skip_set),
        "fetched":    fetched,
        "failed":     failed,
        "total_rows": total_rows,
    }
    logger.info("yfinance backfill complete: %s", summary)
    return summary
