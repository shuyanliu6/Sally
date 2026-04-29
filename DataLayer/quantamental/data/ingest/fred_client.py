import logging
from datetime import date, timedelta

import pandas as pd
from fredapi import Fred

from config.settings import FRED_API_KEY
from config.universe import FRED_SERIES

logger = logging.getLogger(__name__)

_fred: Fred | None = None
_cache: dict[str, pd.Series] = {}


def _get_fred() -> Fred:
    global _fred
    if _fred is None:
        _fred = Fred(api_key=FRED_API_KEY)
    return _fred


def fetch_series(
    series_id: str,
    start: date | str | None = None,
    end: date | str | None = None,
) -> pd.DataFrame:
    """Fetch a FRED series and return a DataFrame with columns [ts, value].

    Falls back to the last cached value if the API call fails.
    """
    start_str = str(start) if start else "2020-01-01"
    end_str = str(end) if end else date.today().isoformat()

    try:
        fred = _get_fred()
        series: pd.Series = fred.get_series(series_id, observation_start=start_str, observation_end=end_str)
        _cache[series_id] = series
        logger.info("Fetched FRED %s: %d observations", series_id, len(series))
    except Exception as exc:
        logger.warning("FRED fetch failed for %s: %s — using cache", series_id, exc)
        series = _cache.get(series_id, pd.Series(dtype=float))

    df = series.dropna().reset_index()
    df.columns = ["ts", "value"]
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize("UTC")
    return df


def fetch_all_macro(
    start: date | str | None = None,
    end: date | str | None = None,
) -> dict[str, pd.DataFrame]:
    """Fetch all four macro indicators. Returns {indicator_name: DataFrame}."""
    result = {}
    for name, series_id in FRED_SERIES.items():
        df = fetch_series(series_id, start=start, end=end)
        result[name] = df
    return result


def last_known_value(series_id: str) -> float | None:
    """Return the most recent non-null value for a series from cache or FRED."""
    series = _cache.get(series_id)
    if series is None:
        try:
            series = _get_fred().get_series(series_id)
            _cache[series_id] = series
        except Exception:
            return None
    s = series.dropna()
    return float(s.iloc[-1]) if not s.empty else None
