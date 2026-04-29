import logging
from datetime import UTC, datetime

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from config.settings import (
    QUESTDB_HOST, QUESTDB_PG_PORT, QUESTDB_USER, QUESTDB_PASSWORD, QUESTDB_DATABASE,
)

logger = logging.getLogger(__name__)

# Cached SQLAlchemy engine — used for read queries via pandas.
# Writes still use raw psycopg2 + execute_values (faster bulk inserts).
_engine: Engine | None = None


def _get_engine() -> Engine:
    """Return a cached SQLAlchemy engine for QuestDB (PostgreSQL wire protocol)."""
    global _engine
    if _engine is None:
        url = (
            f"postgresql+psycopg2://{QUESTDB_USER}:{QUESTDB_PASSWORD}"
            f"@{QUESTDB_HOST}:{QUESTDB_PG_PORT}/{QUESTDB_DATABASE}"
        )
        # pool_pre_ping handles dropped connections gracefully.
        # pool_recycle prevents stale-connection errors after long idle gaps.
        _engine = create_engine(url, pool_pre_ping=True, pool_recycle=3600)
    return _engine

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

# Idempotent migration to add confirmed_regime to existing tables (D5 fix).
ALTER_REGIME_SIGNALS_ADD_CONFIRMED = """
ALTER TABLE regime_signals ADD COLUMN confirmed_regime STRING;
"""

# ── Month 2 schemas ───────────────────────────────────────────────────────────

# Sector-level (one row per day)
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

# Stock-level (one row per ticker per day) — CAPACITY 2048 to match daily_ohlcv
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

# Portfolio-level composite signal (one row per day)
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

# Event flags (golden/death cross, earnings beats/misses, etc.)
CREATE_SIGNAL_EVENTS = """
CREATE TABLE IF NOT EXISTS signal_events (
    symbol SYMBOL CAPACITY 2048 INDEX,
    ts TIMESTAMP,
    event_type STRING,
    details STRING,
    signal_impact INT
) TIMESTAMP(ts) PARTITION BY MONTH;
"""

# Quarterly fundamentals (one row per ticker per fiscal period)
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


def get_connection():
    return psycopg2.connect(
        host=QUESTDB_HOST,
        port=QUESTDB_PG_PORT,
        user=QUESTDB_USER,
        password=QUESTDB_PASSWORD,
        database=QUESTDB_DATABASE,
    )


def init_schema():
    """Create all tables. Idempotent (CREATE IF NOT EXISTS)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Month 1 tables
            cur.execute(CREATE_DAILY_OHLCV)
            cur.execute(CREATE_MACRO_INDICATORS)
            cur.execute(CREATE_REGIME_SIGNALS)
            # Month 2 tables
            cur.execute(CREATE_SECTOR_SIGNALS)
            cur.execute(CREATE_STOCK_SIGNALS)
            cur.execute(CREATE_COMPOSITE_SIGNALS)
            cur.execute(CREATE_SIGNAL_EVENTS)
            cur.execute(CREATE_FUNDAMENTALS)
        conn.commit()

    # Idempotent migrations — apply column additions for tables that may
    # predate the schema change. Safe to run on every init.
    _try_alter("regime_signals", ALTER_REGIME_SIGNALS_ADD_CONFIRMED)

    logger.info("QuestDB schema initialised (Month 1 + Month 2 tables)")


def _try_alter(table: str, sql: str):
    """Run an ALTER statement, swallowing 'column already exists' errors.

    QuestDB doesn't have a portable IF NOT EXISTS syntax for ADD COLUMN,
    so we attempt and catch. Other errors (auth, connection) propagate.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
        logger.info("Applied schema migration: %s", sql.strip().split('\n')[0][:80])
    except Exception as exc:
        msg = str(exc).lower()
        if "exists" in msg or "duplicate" in msg:
            return  # already applied — fine
        logger.warning("ALTER %s skipped (%s): %s", table, type(exc).__name__, exc)


def recreate_ohlcv_table():
    """Drop and recreate daily_ohlcv with the current SYMBOL CAPACITY.

    QuestDB does not support ALTER COLUMN for SYMBOL capacity, so this is the
    only way to upsize. Destructive — use only when migrating between universe
    sizes (e.g. 27 candidates → ~1,200 research universe). Backfill should
    follow immediately after.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS daily_ohlcv;")
            cur.execute(CREATE_DAILY_OHLCV)
        conn.commit()
    logger.warning("daily_ohlcv table dropped and recreated with new schema")


def write_ohlcv(df: pd.DataFrame):
    """Insert rows into daily_ohlcv. df must have columns matching the table schema."""
    rows = [
        (
            row["symbol"],
            row["ts"].isoformat() if hasattr(row["ts"], "isoformat") else row["ts"],
            row.get("open"),
            row.get("high"),
            row.get("low"),
            row.get("close"),
            int(row.get("volume", 0)),
            row.get("vwap"),
            int(row.get("num_trades", 0)),
        )
        for _, row in df.iterrows()
    ]
    sql = """
        INSERT INTO daily_ohlcv (symbol, ts, open, high, low, close, volume, vwap, num_trades)
        VALUES %s
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, rows)
        conn.commit()
    logger.info("Wrote %d OHLCV rows", len(rows))


def latest_macro_ts(indicator: str) -> pd.Timestamp | None:
    """Return the most recent ts for a macro indicator, or None if no rows."""
    df = query(
        f"SELECT max(ts) AS latest FROM macro_indicators WHERE indicator = '{indicator}'"
    )
    if df.empty or pd.isna(df["latest"].iloc[0]):
        return None
    return pd.Timestamp(df["latest"].iloc[0]).tz_localize("UTC") if pd.Timestamp(df["latest"].iloc[0]).tz is None else pd.Timestamp(df["latest"].iloc[0])


def latest_signal_ts() -> pd.Timestamp | None:
    """Return the most recent ts from regime_signals, or None if empty."""
    df = query("SELECT max(ts) AS latest FROM regime_signals")
    if df.empty or pd.isna(df["latest"].iloc[0]):
        return None
    return pd.Timestamp(df["latest"].iloc[0])


def latest_regime_pair() -> tuple[str | None, str | None]:
    """Return (regime, confirmed_regime) of the most recent signal, or (None, None) if empty.

    Used by D5 (2-day regime confirmation) — the aggregator needs yesterday's
    raw and confirmed regimes to compute today's confirmed_regime.
    """
    try:
        df = query(
            "SELECT regime, confirmed_regime FROM regime_signals "
            "ORDER BY ts DESC LIMIT 1"
        )
    except Exception:
        return (None, None)
    if df.empty:
        return (None, None)
    raw = df["regime"].iloc[0] if pd.notna(df["regime"].iloc[0]) else None
    confirmed = df["confirmed_regime"].iloc[0] if pd.notna(df["confirmed_regime"].iloc[0]) else None
    return (raw, confirmed)


def write_macro(df: pd.DataFrame, indicator: str):
    """Insert rows into macro_indicators for a given indicator name.

    Skips rows whose ts is <= the latest ts already in the table (D1 dedup).
    """
    if df.empty:
        logger.info("write_macro: no rows to write for %s", indicator)
        return

    latest = latest_macro_ts(indicator)
    if latest is not None:
        # Normalise both sides to tz-aware UTC for safe comparison
        df_ts = pd.to_datetime(df["ts"])
        if df_ts.dt.tz is None:
            df_ts = df_ts.dt.tz_localize("UTC")
        latest_utc = latest if latest.tz is not None else latest.tz_localize("UTC")
        keep = df_ts > latest_utc
        skipped = (~keep).sum()
        df = df[keep.values]
        if skipped:
            logger.info("write_macro %s: skipped %d existing rows (latest in DB: %s)",
                        indicator, skipped, latest_utc)

    if df.empty:
        logger.info("write_macro %s: nothing new to insert", indicator)
        return

    rows = [
        (
            indicator,
            row["ts"].isoformat() if hasattr(row["ts"], "isoformat") else row["ts"],
            row.get("value"),
            row.get("ma_20"),
            row.get("ma_60"),
            int(row.get("signal", 0)),
        )
        for _, row in df.iterrows()
    ]
    sql = """
        INSERT INTO macro_indicators (indicator, ts, value, ma_20, ma_60, signal)
        VALUES %s
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, rows)
        conn.commit()
    logger.info("Wrote %d new macro rows for %s", len(rows), indicator)


def write_signals(row: dict):
    """Insert one row into regime_signals.

    Skips insert if a signal already exists for the same calendar day (D1 dedup).
    """
    ts = row.get("ts", datetime.now(UTC))
    if hasattr(ts, "isoformat"):
        ts_obj = pd.Timestamp(ts)
    else:
        ts_obj = pd.Timestamp(ts)

    target_date = ts_obj.date()
    existing = query(
        f"SELECT count() AS n FROM regime_signals "
        f"WHERE ts >= '{target_date}' AND ts < '{target_date + pd.Timedelta(days=1)}'"
    )
    if not existing.empty and int(existing["n"].iloc[0]) > 0:
        logger.info("write_signals: signal already exists for %s — skipping", target_date)
        return

    sql = """
        INSERT INTO regime_signals
            (ts, yield_10y_signal, vix_signal, fed_bs_signal, credit_spread_signal,
             composite_score, regime, confirmed_regime)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """
    values = (
        ts.isoformat() if hasattr(ts, "isoformat") else ts,
        row["yield_10y_signal"],
        row["vix_signal"],
        row["fed_bs_signal"],
        row["credit_spread_signal"],
        row["composite_score"],
        row["regime"],
        row.get("confirmed_regime", row["regime"]),  # default to today's if not provided
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
        conn.commit()
    logger.info("Wrote regime signal: %s (score=%d)", row["regime"], row["composite_score"])


def query(sql: str) -> pd.DataFrame:
    """Run a read-only SQL query and return a DataFrame.

    Uses a cached SQLAlchemy engine — silences the pandas warning about raw
    DBAPI connections and is the officially supported integration path.
    """
    engine = _get_engine()
    with engine.connect() as conn:
        return pd.read_sql_query(text(sql), conn)


def latest_ohlcv_date(symbol: str) -> str | None:
    """Return the latest ts for a symbol, or None if no data."""
    df = query(f"SELECT MAX(ts) AS latest FROM daily_ohlcv WHERE symbol = '{symbol}'")
    val = df["latest"].iloc[0]
    return val if pd.notna(val) else None


# ── Dashboard read helpers (sector + stock signals + OHLCV history) ───────────

def get_sector_signal_history(days: int = 90) -> pd.DataFrame:
    """Recent sector_signals rows ordered by ts asc, for charting."""
    return query(
        f"SELECT * FROM sector_signals "
        f"WHERE ts >= dateadd('d', -{int(days)}, now()) ORDER BY ts ASC"
    )


def get_latest_sector_signals() -> pd.Series | None:
    """Most recent sector_signals row, or None if the table is empty."""
    try:
        df = query("SELECT * FROM sector_signals ORDER BY ts DESC LIMIT 1")
    except Exception:
        return None
    return None if df.empty else df.iloc[0]


def get_stock_signal_history(symbol: str, days: int = 180) -> pd.DataFrame:
    """Per-ticker stock_signals history for the chart panel."""
    safe = symbol.replace("'", "''")
    return query(
        f"SELECT * FROM stock_signals WHERE symbol = '{safe}' "
        f"AND ts >= dateadd('d', -{int(days)}, now()) ORDER BY ts ASC"
    )


def get_latest_stock_signals(symbols: list[str]) -> pd.DataFrame:
    """Latest stock_signals row per symbol for a list of tickers."""
    if not symbols:
        return pd.DataFrame()
    in_list = ",".join("'" + s.replace("'", "''") + "'" for s in symbols)
    return query(
        f"SELECT * FROM stock_signals WHERE symbol IN ({in_list}) "
        f"LATEST ON ts PARTITION BY symbol"
    )


def get_ohlcv_history(symbol: str, days: int = 180) -> pd.DataFrame:
    """Per-ticker daily OHLCV history for the chart panel."""
    safe = symbol.replace("'", "''")
    return query(
        f"SELECT ts, open, high, low, close, volume FROM daily_ohlcv "
        f"WHERE symbol = '{safe}' "
        f"AND ts >= dateadd('d', -{int(days)}, now()) ORDER BY ts ASC"
    )


# ── Month 2 write helpers ─────────────────────────────────────────────────────

def write_sector_signals(row: dict):
    """Insert one row into sector_signals. Skips dup if same calendar day exists."""
    ts = row.get("ts", datetime.now(UTC))
    target_date = pd.Timestamp(ts).date()
    existing = query(
        f"SELECT count() AS n FROM sector_signals "
        f"WHERE ts >= '{target_date}' AND ts < '{target_date + pd.Timedelta(days=1)}'"
    )
    if not existing.empty and int(existing["n"].iloc[0]) > 0:
        logger.info("write_sector_signals: row exists for %s — skipping", target_date)
        return

    sql = """
        INSERT INTO sector_signals
            (ts, sox_spx_ratio, sox_spx_ema20, sox_spx_ema60, sox_spx_signal,
             tsmc_signal, capex_signal, api_pricing_signal, sector_composite)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    values = (
        ts.isoformat() if hasattr(ts, "isoformat") else ts,
        row.get("sox_spx_ratio"),
        row.get("sox_spx_ema20"),
        row.get("sox_spx_ema60"),
        int(row.get("sox_spx_signal", 0)),
        int(row.get("tsmc_signal", 0)),
        int(row.get("capex_signal", 0)),
        int(row.get("api_pricing_signal", 0)),
        int(row.get("sector_composite", 0)),
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
        conn.commit()
    logger.info("Wrote sector signal: composite=%d", row.get("sector_composite", 0))


def write_stock_signals(df: pd.DataFrame):
    """Bulk-insert per-ticker stock signal rows for one day.

    df columns: symbol, ts, close, ema_20, ema_60, ema_signal, rsi_14, rsi_signal,
                volume_ratio, volume_signal, pead_signal, stock_composite
    Skips (symbol, day) pairs already present.
    """
    if df.empty:
        return

    # Find which symbol/day pairs are already in the table for this date
    target_date = pd.Timestamp(df["ts"].iloc[0]).date()
    try:
        existing = query(
            f"SELECT symbol FROM stock_signals "
            f"WHERE ts >= '{target_date}' AND ts < '{target_date + pd.Timedelta(days=1)}'"
        )
        existing_syms = set(existing["symbol"]) if not existing.empty else set()
    except Exception:
        existing_syms = set()

    new_df = df[~df["symbol"].isin(existing_syms)]
    if new_df.empty:
        logger.info("write_stock_signals: all %d rows for %s already exist — skipping",
                    len(df), target_date)
        return

    rows = [
        (
            r["symbol"],
            r["ts"].isoformat() if hasattr(r["ts"], "isoformat") else r["ts"],
            float(r.get("close") or 0),
            float(r.get("ema_20") or 0),
            float(r.get("ema_60") or 0),
            int(r.get("ema_signal") or 0),
            float(r.get("rsi_14") or 0),
            int(r.get("rsi_signal") or 0),
            float(r.get("volume_ratio") or 0),
            int(r.get("volume_signal") or 0),
            int(r.get("pead_signal") or 0),
            int(r.get("stock_composite") or 0),
        )
        for _, r in new_df.iterrows()
    ]
    sql = """
        INSERT INTO stock_signals
            (symbol, ts, close, ema_20, ema_60, ema_signal, rsi_14, rsi_signal,
             volume_ratio, volume_signal, pead_signal, stock_composite)
        VALUES %s
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, rows)
        conn.commit()
    logger.info("Wrote %d stock signal rows for %s", len(rows), target_date)


def write_composite_signal(row: dict):
    """Insert one row into composite_signals. Skips dup if same day exists."""
    ts = row.get("ts", datetime.now(UTC))
    target_date = pd.Timestamp(ts).date()
    existing = query(
        f"SELECT count() AS n FROM composite_signals "
        f"WHERE ts >= '{target_date}' AND ts < '{target_date + pd.Timedelta(days=1)}'"
    )
    if not existing.empty and int(existing["n"].iloc[0]) > 0:
        logger.info("write_composite_signal: row exists for %s — skipping", target_date)
        return

    sql = """
        INSERT INTO composite_signals
            (ts, macro_score, sector_score, avg_stock_score,
             weighted_composite, normalized_score, regime, action)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """
    values = (
        ts.isoformat() if hasattr(ts, "isoformat") else ts,
        int(row.get("macro_score", 0)),
        int(row.get("sector_score", 0)),
        float(row.get("avg_stock_score", 0)),
        float(row.get("weighted_composite", 0)),
        float(row.get("normalized_score", 0)),
        row.get("regime", "NEUTRAL"),
        row.get("action", "HOLD"),
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
        conn.commit()
    logger.info("Wrote composite signal: regime=%s action=%s",
                row.get("regime"), row.get("action"))


def write_signal_event(symbol: str, event_type: str, details: str = "",
                       signal_impact: int = 0, ts: datetime | None = None):
    """Insert one row into signal_events (golden cross, earnings beat, etc.)."""
    ts = ts or datetime.now(UTC)
    sql = """
        INSERT INTO signal_events (symbol, ts, event_type, details, signal_impact)
        VALUES (%s, %s, %s, %s, %s)
    """
    values = (
        symbol,
        ts.isoformat() if hasattr(ts, "isoformat") else ts,
        event_type, details, int(signal_impact),
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
        conn.commit()
    logger.info("Event: %s %s (impact=%d)", symbol, event_type, signal_impact)


def write_fundamentals(df: pd.DataFrame):
    """Bulk-insert quarterly fundamentals.

    df columns: symbol, period_end, fiscal_period, revenue, net_income,
                eps_basic, eps_diluted, total_assets, total_debt,
                operating_cash_flow, free_cash_flow, shares_outstanding
    Skips (symbol, period_end) pairs already present.
    """
    if df.empty:
        return

    # Dedup against existing
    try:
        existing = query("SELECT symbol, period_end FROM fundamentals")
        existing_pairs = set(zip(existing["symbol"], existing["period_end"].astype(str)))
    except Exception:
        existing_pairs = set()

    df = df.copy()
    df["pair"] = list(zip(df["symbol"], df["period_end"].astype(str)))
    new_df = df[~df["pair"].isin(existing_pairs)].drop(columns=["pair"])

    if new_df.empty:
        logger.info("write_fundamentals: all rows already present — skipping")
        return

    rows = [
        (
            r["symbol"],
            r["period_end"].isoformat() if hasattr(r["period_end"], "isoformat") else r["period_end"],
            r.get("fiscal_period"),
            float(r.get("revenue") or 0) if pd.notna(r.get("revenue")) else None,
            float(r.get("net_income") or 0) if pd.notna(r.get("net_income")) else None,
            float(r.get("eps_basic") or 0) if pd.notna(r.get("eps_basic")) else None,
            float(r.get("eps_diluted") or 0) if pd.notna(r.get("eps_diluted")) else None,
            float(r.get("total_assets") or 0) if pd.notna(r.get("total_assets")) else None,
            float(r.get("total_debt") or 0) if pd.notna(r.get("total_debt")) else None,
            float(r.get("operating_cash_flow") or 0) if pd.notna(r.get("operating_cash_flow")) else None,
            float(r.get("free_cash_flow") or 0) if pd.notna(r.get("free_cash_flow")) else None,
            int(r.get("shares_outstanding") or 0) if pd.notna(r.get("shares_outstanding")) else None,
            datetime.now(UTC).isoformat(),
        )
        for _, r in new_df.iterrows()
    ]
    sql = """
        INSERT INTO fundamentals
            (symbol, period_end, fiscal_period, revenue, net_income,
             eps_basic, eps_diluted, total_assets, total_debt,
             operating_cash_flow, free_cash_flow, shares_outstanding, fetched_at)
        VALUES %s
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(cur, sql, rows)
        conn.commit()
    logger.info("Wrote %d new fundamentals rows", len(rows))
