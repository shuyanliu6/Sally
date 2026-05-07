import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from quantamental.data.ingest.questdb_connection import get_connection, query

logger = logging.getLogger(__name__)

CREATE_DAILY_OHLCV = """
CREATE TABLE IF NOT EXISTS daily_ohlcv (
    symbol SYMBOL CAPACITY 2048 INDEX,
    ts TIMESTAMP,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume LONG,
    vwap DOUBLE,
    num_trades LONG
) TIMESTAMP(ts) PARTITION BY MONTH;
"""

CREATE_MACRO_INDICATORS = """
CREATE TABLE IF NOT EXISTS macro_indicators (
    indicator SYMBOL CAPACITY 16 INDEX,
    ts TIMESTAMP,
    value DOUBLE,
    ma_20 DOUBLE,
    ma_60 DOUBLE,
    signal INT
) TIMESTAMP(ts) PARTITION BY MONTH;
"""

CREATE_REGIME_SIGNALS = """
CREATE TABLE IF NOT EXISTS regime_signals (
    ts TIMESTAMP,
    yield_10y_signal INT,
    vix_signal INT,
    fed_bs_signal INT,
    credit_spread_signal INT,
    composite_score INT,
    regime STRING,
    confirmed_regime STRING
) TIMESTAMP(ts) PARTITION BY MONTH;
"""

CREATE_SECTOR_SIGNALS = """
CREATE TABLE IF NOT EXISTS sector_signals (
    ts TIMESTAMP,
    sox_spx_ratio DOUBLE,
    sox_spx_ema20 DOUBLE,
    sox_spx_ema60 DOUBLE,
    sox_spx_signal INT,
    tsmc_signal INT,
    capex_signal INT,
    api_pricing_signal INT,
    sector_composite INT
) TIMESTAMP(ts) PARTITION BY MONTH;
"""

CREATE_STOCK_SIGNALS = """
CREATE TABLE IF NOT EXISTS stock_signals (
    symbol SYMBOL CAPACITY 2048 INDEX,
    ts TIMESTAMP,
    close DOUBLE,
    ema_20 DOUBLE,
    ema_60 DOUBLE,
    ema_signal INT,
    rsi_14 DOUBLE,
    rsi_signal INT,
    volume_ratio DOUBLE,
    volume_signal INT,
    pead_signal INT,
    stock_composite INT
) TIMESTAMP(ts) PARTITION BY MONTH;
"""

CREATE_COMPOSITE_SIGNALS = """
CREATE TABLE IF NOT EXISTS composite_signals (
    ts TIMESTAMP,
    macro_score INT,
    sector_score INT,
    avg_stock_score DOUBLE,
    weighted_composite DOUBLE,
    normalized_score DOUBLE,
    regime STRING,
    action STRING
) TIMESTAMP(ts) PARTITION BY MONTH;
"""

CREATE_SIGNAL_EVENTS = """
CREATE TABLE IF NOT EXISTS signal_events (
    symbol SYMBOL CAPACITY 2048 INDEX,
    ts TIMESTAMP,
    event_type STRING,
    details STRING,
    signal_impact INT
) TIMESTAMP(ts) PARTITION BY MONTH;
"""

CREATE_FUNDAMENTALS = """
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol SYMBOL CAPACITY 2048 INDEX,
    period_end TIMESTAMP,
    fiscal_period STRING,
    revenue DOUBLE,
    net_income DOUBLE,
    eps_basic DOUBLE,
    eps_diluted DOUBLE,
    total_assets DOUBLE,
    total_debt DOUBLE,
    operating_cash_flow DOUBLE,
    free_cash_flow DOUBLE,
    shares_outstanding LONG,
    fetched_at TIMESTAMP
) TIMESTAMP(period_end) PARTITION BY YEAR;
"""

CREATE_SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version STRING,
    applied_at TIMESTAMP,
    description STRING
) TIMESTAMP(applied_at) PARTITION BY YEAR;
"""


@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    sql: str
    tolerate_duplicate: bool = False


MIGRATIONS = (
    Migration(
        version="001_confirmed_regime",
        description="Add confirmed_regime to regime_signals",
        sql="ALTER TABLE regime_signals ADD COLUMN confirmed_regime STRING;",
        tolerate_duplicate=True,
    ),
)


def _is_duplicate_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "exists" in msg or "duplicate" in msg


def applied_migrations() -> set[str]:
    try:
        df = query("SELECT version FROM schema_migrations")
    except Exception:
        return set()
    if df.empty or "version" not in df:
        return set()
    return set(df["version"].dropna().astype(str))


def record_migration(version: str, description: str) -> None:
    sql = """
        INSERT INTO schema_migrations (version, applied_at, description)
        VALUES (%s, %s, %s)
    """
    values = (version, datetime.now(UTC).isoformat(), description)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
        conn.commit()


def apply_migration(migration: Migration, applied: set[str] | None = None) -> bool:
    already_applied = applied if applied is not None else applied_migrations()
    if migration.version in already_applied:
        return False

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(migration.sql)
            conn.commit()
    except Exception as exc:
        if not (migration.tolerate_duplicate and _is_duplicate_error(exc)):
            raise
        logger.info("Migration %s already reflected in schema", migration.version)

    record_migration(migration.version, migration.description)
    logger.info("Applied migration %s: %s", migration.version, migration.description)
    return True


def apply_migrations() -> list[str]:
    applied = applied_migrations()
    applied_now = []
    for migration in MIGRATIONS:
        if apply_migration(migration, applied=applied):
            applied.add(migration.version)
            applied_now.append(migration.version)
    return applied_now


def init_schema():
    """Create all tables and apply non-destructive schema migrations."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_DAILY_OHLCV)
            cur.execute(CREATE_MACRO_INDICATORS)
            cur.execute(CREATE_REGIME_SIGNALS)
            cur.execute(CREATE_SECTOR_SIGNALS)
            cur.execute(CREATE_STOCK_SIGNALS)
            cur.execute(CREATE_COMPOSITE_SIGNALS)
            cur.execute(CREATE_SIGNAL_EVENTS)
            cur.execute(CREATE_FUNDAMENTALS)
            cur.execute(CREATE_SCHEMA_MIGRATIONS)
        conn.commit()

    apply_migrations()
    logger.info("QuestDB schema initialised (tables + migrations)")


def recreate_ohlcv_table():
    """Drop and recreate daily_ohlcv with the current SYMBOL CAPACITY.

    Destructive by design. Keep this manual and explicit.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS daily_ohlcv;")
            cur.execute(CREATE_DAILY_OHLCV)
        conn.commit()
    logger.warning("daily_ohlcv table dropped and recreated with new schema")

