import logging
import threading
import time
from datetime import date, timedelta

import pandas as pd
from polygon import RESTClient
from polygon.rest.models import Agg

from config.settings import (
    POLYGON_API_KEY,
    POLYGON_RATE_LIMIT_BACKOFF,
    POLYGON_REQUESTS_PER_MINUTE,
)

logger = logging.getLogger(__name__)


# ── Rate limiter ──────────────────────────────────────────────────────────────

class _RateLimiter:
    """Token-bucket rate limiter. Thread-safe."""

    def __init__(self, requests_per_minute: int):
        self._min_interval = 60.0 / max(requests_per_minute, 1)
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            wait_for = self._min_interval - elapsed
            if wait_for > 0:
                time.sleep(wait_for)
            self._last_call = time.monotonic()


_limiter = _RateLimiter(POLYGON_REQUESTS_PER_MINUTE)
_client: RESTClient | None = None


def _get_client() -> RESTClient:
    global _client
    if _client is None:
        _client = RESTClient(api_key=POLYGON_API_KEY)
    return _client


def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "too many" in msg or "rate limit" in msg


# ── Helpers ───────────────────────────────────────────────────────────────────

# Cache the NYSE calendar at module load — creating it is non-trivial
try:
    import pandas_market_calendars as _mcal
    _NYSE = _mcal.get_calendar("XNYS")
    logger.info("NYSE market calendar loaded — prev_trading_day is holiday-aware")
except Exception as _exc:  # pragma: no cover
    _NYSE = None
    logger.warning(
        "pandas_market_calendars unavailable (%s) — prev_trading_day will use "
        "weekday-only fallback (will incorrectly include market holidays)",
        _exc,
    )


def prev_trading_day(from_date: date | None = None) -> date:
    """Return the most recent completed NYSE trading day, skipping today.

    Uses pandas_market_calendars XNYS calendar so US market holidays
    (Christmas, Thanksgiving, July 4, MLK Day, etc.) are correctly skipped.
    Falls back to weekday-only logic if the calendar package is unavailable.
    """
    ref = (from_date or date.today())
    yesterday = ref - timedelta(days=1)

    if _NYSE is not None:
        # Look back 14 days — covers the longest weekend+holiday gap (e.g. Thanksgiving)
        start = yesterday - timedelta(days=14)
        valid = _NYSE.valid_days(start_date=start.isoformat(), end_date=yesterday.isoformat())
        if len(valid):
            return valid[-1].date()
        # Calendar empty — fall through to weekday logic

    # Fallback: weekday-only
    d = yesterday
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _parse_agg(ticker: str, agg: Agg) -> dict:
    return {
        "symbol":     ticker,
        "ts":         pd.Timestamp(agg.timestamp, unit="ms", tz="UTC").floor("s"),
        "open":       agg.open,
        "high":       agg.high,
        "low":        agg.low,
        "close":      agg.close,
        "volume":     int(agg.volume or 0),
        "vwap":       agg.vwap,
        "num_trades": int(agg.transactions or 0),
    }


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_grouped_daily(
    target_date: date | str,
    tickers: list[str] | None = None,
    retries: int = 3,
) -> pd.DataFrame:
    """Fetch end-of-day OHLCV for ALL US stocks in ONE API call.

    Massively faster than fetch_daily_ohlcv for the daily pipeline:
        27 tickers × 12s = 5.5 minutes  →  1 call ≈ 3 seconds

    Args:
        target_date: trading day to fetch
        tickers: optional list to filter results (returns ALL tickers if None)
        retries: retry attempts on transient failures

    Returns:
        DataFrame with columns: symbol, ts, open, high, low, close, volume, vwap, num_trades
    """
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    date_str = target_date.isoformat()
    client = _get_client()

    universe_filter = set(tickers) if tickers else None

    for attempt in range(1, retries + 1):
        try:
            _limiter.wait()
            aggs = client.get_grouped_daily_aggs(
                date=date_str,
                adjusted=True,
                include_otc=False,
            )
        except Exception as exc:
            if _is_rate_limit_error(exc):
                logger.warning(
                    "grouped_daily: 429 — waiting %ds (attempt %d/%d)",
                    POLYGON_RATE_LIMIT_BACKOFF, attempt, retries,
                )
                time.sleep(POLYGON_RATE_LIMIT_BACKOFF)
                continue
            backoff = 5 * attempt
            logger.warning(
                "grouped_daily attempt %d/%d failed: %s — retrying in %ds",
                attempt, retries, exc, backoff,
            )
            time.sleep(backoff)
            continue

        records = []
        for agg in aggs:
            sym = getattr(agg, "ticker", None)
            if sym is None:
                continue
            if universe_filter and sym not in universe_filter:
                continue
            records.append({
                "symbol":     sym,
                "ts":         pd.Timestamp(agg.timestamp, unit="ms", tz="UTC").floor("s"),
                "open":       agg.open,
                "high":       agg.high,
                "low":        agg.low,
                "close":      agg.close,
                "volume":     int(agg.volume or 0),
                "vwap":       getattr(agg, "vwap", None),
                "num_trades": int(getattr(agg, "transactions", 0) or 0),
            })

        df = pd.DataFrame(records)

        if universe_filter:
            found_syms = set(df["symbol"]) if not df.empty else set()
            missing    = universe_filter - found_syms
            if missing:
                logger.warning(
                    "grouped_daily: %d/%d tickers missing on %s: %s",
                    len(missing), len(universe_filter), date_str, ", ".join(sorted(missing)),
                )

        logger.info(
            "grouped_daily: 1 API call returned %d rows for %s (%d after universe filter)",
            len(aggs), date_str, len(df),
        )
        return df

    logger.error("grouped_daily: gave up after %d attempts for %s", retries, date_str)
    return pd.DataFrame()


def fetch_daily_ohlcv(
    tickers: list[str],
    target_date: date | str,
    retries: int = 3,
) -> pd.DataFrame:
    """Fetch end-of-day OHLCV for each ticker on target_date.

    Respects POLYGON_REQUESTS_PER_MINUTE (default 5 for free tier).
    On 429, backs off POLYGON_RATE_LIMIT_BACKOFF seconds before retrying.

    Returns a DataFrame with columns:
        symbol, ts, open, high, low, close, volume, vwap, num_trades
    """
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)

    date_str = target_date.isoformat()
    client   = _get_client()
    records  = []
    failed   = []

    logger.info(
        "Fetching %d tickers for %s (rate limit: %d req/min)",
        len(tickers), date_str, POLYGON_REQUESTS_PER_MINUTE,
    )

    for i, ticker in enumerate(tickers, 1):
        success = False
        for attempt in range(1, retries + 1):
            try:
                _limiter.wait()   # enforce rate limit before every call
                aggs: list[Agg] = list(
                    client.list_aggs(
                        ticker=ticker,
                        multiplier=1,
                        timespan="day",
                        from_=date_str,
                        to=date_str,
                        adjusted=True,
                        limit=1,
                    )
                )
                if aggs:
                    records.append(_parse_agg(ticker, aggs[0]))
                else:
                    logger.warning("[%d/%d] %s: no data on %s (market closed?)",
                                   i, len(tickers), ticker, date_str)
                success = True
                break

            except Exception as exc:
                if _is_rate_limit_error(exc):
                    logger.warning(
                        "[%d/%d] %s: 429 rate-limited — waiting %ds before retry %d/%d",
                        i, len(tickers), ticker, POLYGON_RATE_LIMIT_BACKOFF, attempt, retries,
                    )
                    time.sleep(POLYGON_RATE_LIMIT_BACKOFF)
                else:
                    backoff = 5 * attempt
                    logger.warning(
                        "[%d/%d] %s attempt %d/%d failed: %s — retrying in %ds",
                        i, len(tickers), ticker, attempt, retries, exc, backoff,
                    )
                    time.sleep(backoff)

        if not success:
            logger.error("[%d/%d] %s: gave up after %d attempts", i, len(tickers), ticker, retries)
            failed.append(ticker)

    if failed:
        logger.warning("Failed tickers (%d): %s", len(failed), ", ".join(failed))

    df = pd.DataFrame(records)
    logger.info("Fetched %d/%d tickers for %s", len(records), len(tickers), date_str)
    return df


def fetch_date_range(
    tickers: list[str],
    start: date | str,
    end: date | str,
    retries: int = 3,
) -> pd.DataFrame:
    """Fetch OHLCV for all tickers over a date range (used by backfill).

    One API call per ticker (returns all days in range). Still rate-limited.
    """
    if isinstance(start, str):
        start = date.fromisoformat(start)
    if isinstance(end, str):
        end = date.fromisoformat(end)

    client  = _get_client()
    records = []
    failed  = []

    for i, ticker in enumerate(tickers, 1):
        success = False
        for attempt in range(1, retries + 1):
            try:
                _limiter.wait()
                aggs = list(
                    client.list_aggs(
                        ticker=ticker,
                        multiplier=1,
                        timespan="day",
                        from_=start.isoformat(),
                        to=end.isoformat(),
                        adjusted=True,
                        limit=50000,
                    )
                )
                for agg in aggs:
                    records.append(_parse_agg(ticker, agg))
                logger.info("[%d/%d] %s: %d days fetched", i, len(tickers), ticker, len(aggs))
                success = True
                break

            except Exception as exc:
                if _is_rate_limit_error(exc):
                    logger.warning(
                        "[%d/%d] %s: 429 — waiting %ds (attempt %d/%d)",
                        i, len(tickers), ticker, POLYGON_RATE_LIMIT_BACKOFF, attempt, retries,
                    )
                    time.sleep(POLYGON_RATE_LIMIT_BACKOFF)
                else:
                    backoff = 5 * attempt
                    logger.warning(
                        "[%d/%d] %s attempt %d/%d: %s — retrying in %ds",
                        i, len(tickers), ticker, attempt, retries, exc, backoff,
                    )
                    time.sleep(backoff)

        if not success:
            logger.error("[%d/%d] %s: gave up after %d attempts", i, len(tickers), ticker, retries)
            failed.append(ticker)

    if failed:
        logger.warning("Backfill failed tickers (%d): %s", len(failed), ", ".join(failed))

    return pd.DataFrame(records)


def validate_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Flag rows with suspicious daily returns (>20%) for manual review."""
    if df.empty:
        return df
    df = df.copy().sort_values(["symbol", "ts"])
    df["prev_close"]   = df.groupby("symbol")["close"].shift(1)
    df["daily_return"] = (df["close"] - df["prev_close"]) / df["prev_close"].abs()
    suspicious = df[df["daily_return"].abs() > 0.20]
    if not suspicious.empty:
        logger.warning("Suspicious returns (>20%% daily move):\n%s",
                       suspicious[["symbol", "ts", "daily_return"]].to_string())
    return df.drop(columns=["prev_close", "daily_return"])
