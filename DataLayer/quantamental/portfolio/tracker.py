import logging
import sqlite3
from contextlib import contextmanager
from datetime import date

import pandas as pd

from quantamental.config.settings import SQLITE_PATH

logger = logging.getLogger(__name__)

CREATE_POSITIONS = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    entry_price REAL NOT NULL,
    shares REAL NOT NULL,
    target_weight REAL,
    stop_loss_price REAL,
    thesis TEXT,
    status TEXT DEFAULT 'OPEN'
);
"""

CREATE_TRADE_JOURNAL = """
CREATE TABLE IF NOT EXISTS trade_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity REAL,
    price REAL,
    trigger_reason TEXT,
    emotion TEXT,
    thesis_still_valid TEXT,
    notes TEXT,
    review_30d TEXT
);
"""


@contextmanager
def _conn(path: str = SQLITE_PATH):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db(path: str = SQLITE_PATH):
    with _conn(path) as con:
        con.execute(CREATE_POSITIONS)
        con.execute(CREATE_TRADE_JOURNAL)
    logger.info("SQLite schema initialised at %s", path)


def add_position(
    symbol: str,
    entry_date: str | date,
    entry_price: float,
    shares: float,
    target_weight: float | None = None,
    stop_loss_price: float | None = None,
    thesis: str | None = None,
    path: str = SQLITE_PATH,
) -> int:
    sql = """
        INSERT INTO positions (symbol, entry_date, entry_price, shares,
                               target_weight, stop_loss_price, thesis, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN')
    """
    with _conn(path) as con:
        cur = con.execute(sql, (str(symbol), str(entry_date), entry_price, shares,
                                target_weight, stop_loss_price, thesis))
        pos_id = cur.lastrowid
    logger.info("Added position %s id=%d", symbol, pos_id)
    return pos_id


def close_position(pos_id: int, path: str = SQLITE_PATH):
    with _conn(path) as con:
        con.execute("UPDATE positions SET status = 'CLOSED' WHERE id = ?", (pos_id,))
    logger.info("Closed position id=%d", pos_id)


def get_open_positions(path: str = SQLITE_PATH) -> pd.DataFrame:
    with _conn(path) as con:
        rows = con.execute(
            "SELECT * FROM positions WHERE status = 'OPEN'"
        ).fetchall()
    if not rows:
        return pd.DataFrame(columns=[
            "id", "symbol", "entry_date", "entry_price", "shares",
            "target_weight", "stop_loss_price", "thesis", "status",
        ])
    return pd.DataFrame([dict(r) for r in rows])


def compute_pnl(positions_df: pd.DataFrame, latest_prices: dict[str, float]) -> pd.DataFrame:
    """Add current_price, market_value, pnl, pnl_pct, weight columns to positions_df."""
    if positions_df.empty:
        return positions_df

    df = positions_df.copy()
    df["current_price"] = df["symbol"].map(latest_prices)
    df["market_value"] = df["current_price"] * df["shares"]
    df["cost_basis"] = df["entry_price"] * df["shares"]
    df["pnl"] = df["market_value"] - df["cost_basis"]
    df["pnl_pct"] = (df["pnl"] / df["cost_basis"]) * 100

    total_mv = df["market_value"].sum()
    df["weight"] = (df["market_value"] / total_mv * 100) if total_mv > 0 else 0.0

    return df
