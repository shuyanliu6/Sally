import logging
from datetime import UTC, datetime

import pandas as pd
from psycopg2.extras import execute_values

from quantamental.data.ingest.questdb_connection import (
    day_window_params,
    get_connection,
    query,
    symbol_param,
)

logger = logging.getLogger(__name__)


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
        "SELECT max(ts) AS latest FROM macro_indicators WHERE indicator = :indicator",
        {"indicator": indicator},
    )
    if df.empty or pd.isna(df["latest"].iloc[0]):
        return None
    ts = pd.Timestamp(df["latest"].iloc[0])
    return ts.tz_localize("UTC") if ts.tz is None else ts


def latest_signal_ts() -> pd.Timestamp | None:
    """Return the most recent ts from regime_signals, or None if empty."""
    df = query("SELECT max(ts) AS latest FROM regime_signals")
    if df.empty or pd.isna(df["latest"].iloc[0]):
        return None
    return pd.Timestamp(df["latest"].iloc[0])


def latest_regime_pair() -> tuple[str | None, str | None]:
    """Return (regime, confirmed_regime) of the most recent signal."""
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
    """Insert new rows into macro_indicators for a given indicator."""
    if df.empty:
        logger.info("write_macro: no rows to write for %s", indicator)
        return

    latest = latest_macro_ts(indicator)
    if latest is not None:
        df_ts = pd.to_datetime(df["ts"])
        if df_ts.dt.tz is None:
            df_ts = df_ts.dt.tz_localize("UTC")
        latest_utc = latest if latest.tz is not None else latest.tz_localize("UTC")
        keep = df_ts > latest_utc
        skipped = (~keep).sum()
        df = df[keep.values]
        if skipped:
            logger.info(
                "write_macro %s: skipped %d existing rows (latest in DB: %s)",
                indicator, skipped, latest_utc,
            )

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
    """Insert one row into regime_signals, skipping duplicate calendar days."""
    ts = row.get("ts", datetime.now(UTC))
    params = day_window_params(ts)
    existing = query(
        "SELECT count() AS n FROM regime_signals "
        "WHERE ts >= :start AND ts < :end",
        params,
    )
    if not existing.empty and int(existing["n"].iloc[0]) > 0:
        logger.info("write_signals: signal already exists for %s — skipping", params["start"])
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
        row.get("confirmed_regime", row["regime"]),
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
        conn.commit()
    logger.info("Wrote regime signal: %s (score=%d)", row["regime"], row["composite_score"])


def write_sector_signals(row: dict):
    """Insert one row into sector_signals. Skips dup if same calendar day exists."""
    ts = row.get("ts", datetime.now(UTC))
    params = day_window_params(ts)
    existing = query(
        "SELECT count() AS n FROM sector_signals "
        "WHERE ts >= :start AND ts < :end",
        params,
    )
    if not existing.empty and int(existing["n"].iloc[0]) > 0:
        logger.info("write_sector_signals: row exists for %s — skipping", params["start"])
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
    """Bulk-insert per-ticker stock signal rows for one day."""
    if df.empty:
        return

    target_date = pd.Timestamp(df["ts"].iloc[0]).date()
    params = day_window_params(target_date)
    try:
        existing = query(
            "SELECT symbol FROM stock_signals WHERE ts >= :start AND ts < :end",
            params,
        )
        existing_syms = set(existing["symbol"]) if not existing.empty else set()
    except Exception:
        existing_syms = set()

    new_df = df[~df["symbol"].isin(existing_syms)]
    if new_df.empty:
        logger.info(
            "write_stock_signals: all %d rows for %s already exist — skipping",
            len(df), target_date,
        )
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
    params = day_window_params(ts)
    existing = query(
        "SELECT count() AS n FROM composite_signals "
        "WHERE ts >= :start AND ts < :end",
        params,
    )
    if not existing.empty and int(existing["n"].iloc[0]) > 0:
        logger.info("write_composite_signal: row exists for %s — skipping", params["start"])
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
    logger.info("Wrote composite signal: regime=%s action=%s", row.get("regime"), row.get("action"))


def write_signal_event(
    symbol: str,
    event_type: str,
    details: str = "",
    signal_impact: int = 0,
    ts: datetime | None = None,
):
    """Insert one row into signal_events (golden cross, earnings beat, etc.)."""
    ts = ts or datetime.now(UTC)
    sql = """
        INSERT INTO signal_events (symbol, ts, event_type, details, signal_impact)
        VALUES (%s, %s, %s, %s, %s)
    """
    values = (
        symbol_param(symbol)["symbol"],
        ts.isoformat() if hasattr(ts, "isoformat") else ts,
        event_type,
        details,
        int(signal_impact),
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
        conn.commit()
    logger.info("Event: %s %s (impact=%d)", symbol, event_type, signal_impact)


def write_fundamentals(df: pd.DataFrame):
    """Bulk-insert quarterly fundamentals."""
    if df.empty:
        return

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

