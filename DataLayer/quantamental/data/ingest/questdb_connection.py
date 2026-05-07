import logging
from collections.abc import Mapping
from typing import Any

import pandas as pd
import psycopg2
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from quantamental.config.settings import (
    QUESTDB_DATABASE,
    QUESTDB_HOST,
    QUESTDB_PASSWORD,
    QUESTDB_PG_PORT,
    QUESTDB_USER,
)

logger = logging.getLogger(__name__)

_engine: Engine | None = None


def _get_engine() -> Engine:
    """Return a cached SQLAlchemy engine for QuestDB."""
    global _engine
    if _engine is None:
        url = (
            f"postgresql+psycopg2://{QUESTDB_USER}:{QUESTDB_PASSWORD}"
            f"@{QUESTDB_HOST}:{QUESTDB_PG_PORT}/{QUESTDB_DATABASE}"
        )
        _engine = create_engine(url, pool_pre_ping=True, pool_recycle=3600)
    return _engine


def get_connection():
    return psycopg2.connect(
        host=QUESTDB_HOST,
        port=QUESTDB_PG_PORT,
        user=QUESTDB_USER,
        password=QUESTDB_PASSWORD,
        database=QUESTDB_DATABASE,
    )


def query(sql: str, params: Mapping[str, Any] | None = None) -> pd.DataFrame:
    """Run a read-only SQL query and return a DataFrame.

    Args:
        sql: SQLAlchemy text query. Use named placeholders like ``:symbol``.
        params: Optional mapping for named placeholders.
    """
    engine = _get_engine()
    with engine.connect() as conn:
        return pd.read_sql_query(text(sql), conn, params=dict(params or {}))


def coerce_lookback_days(days: int, *, default: int = 90, max_days: int = 3650) -> int:
    """Constrain dynamic dateadd lookbacks before interpolating structural SQL."""
    try:
        value = int(days)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, max_days))


def day_window_params(target_date) -> dict[str, str]:
    """Return inclusive/exclusive ISO date params for one calendar day."""
    ts = pd.Timestamp(target_date)
    start = ts.date()
    end = start + pd.Timedelta(days=1)
    return {"start": str(start), "end": str(end)}


def symbol_param(symbol: str) -> dict[str, str]:
    """Normalize a single ticker for bound SQL params."""
    cleaned = str(symbol).strip().upper()
    if not cleaned:
        raise ValueError("symbol cannot be empty")
    return {"symbol": cleaned}


def symbol_list_clause(symbols: list[str], *, prefix: str = "sym") -> tuple[str, dict[str, str]]:
    """Build a safe IN-list placeholder clause and params.

    SQLAlchemy's expanding bind parameters are not consistently portable through
    pandas' read_sql_query path, so this helper creates named placeholders while
    still binding the actual ticker values as params.
    """
    cleaned = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
    if not cleaned:
        raise ValueError("symbols cannot be empty")
    params = {f"{prefix}_{i}": symbol for i, symbol in enumerate(cleaned)}
    clause = ", ".join(f":{key}" for key in params)
    return clause, params

