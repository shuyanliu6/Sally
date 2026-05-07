import pandas as pd

from quantamental.data.ingest.questdb_connection import (
    coerce_lookback_days,
    query,
    symbol_list_clause,
    symbol_param,
)


def latest_ohlcv_date(symbol: str) -> str | None:
    """Return the latest ts for a symbol, or None if no data."""
    df = query(
        "SELECT MAX(ts) AS latest FROM daily_ohlcv WHERE symbol = :symbol",
        symbol_param(symbol),
    )
    val = df["latest"].iloc[0]
    return val if pd.notna(val) else None


def get_sector_signal_history(days: int = 90) -> pd.DataFrame:
    """Recent sector_signals rows ordered by ts asc, for charting."""
    lookback = coerce_lookback_days(days)
    return query(
        f"SELECT * FROM sector_signals "
        f"WHERE ts >= dateadd('d', -{lookback}, now()) ORDER BY ts ASC"
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
    lookback = coerce_lookback_days(days)
    return query(
        f"SELECT * FROM stock_signals WHERE symbol = :symbol "
        f"AND ts >= dateadd('d', -{lookback}, now()) ORDER BY ts ASC",
        symbol_param(symbol),
    )


def get_latest_stock_signals(symbols: list[str]) -> pd.DataFrame:
    """Latest stock_signals row per symbol for a list of tickers."""
    if not symbols:
        return pd.DataFrame()
    clause, params = symbol_list_clause(symbols)
    return query(
        f"SELECT * FROM stock_signals WHERE symbol IN ({clause}) "
        f"LATEST ON ts PARTITION BY symbol",
        params,
    )


def get_ohlcv_history(symbol: str, days: int = 180) -> pd.DataFrame:
    """Per-ticker daily OHLCV history for the chart panel."""
    lookback = coerce_lookback_days(days)
    return query(
        f"SELECT ts, open, high, low, close, volume FROM daily_ohlcv "
        f"WHERE symbol = :symbol "
        f"AND ts >= dateadd('d', -{lookback}, now()) ORDER BY ts ASC",
        symbol_param(symbol),
    )

