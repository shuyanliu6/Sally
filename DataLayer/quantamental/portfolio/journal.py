import logging
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

import pandas as pd

from config.settings import SQLITE_PATH

logger = logging.getLogger(__name__)

VALID_ACTIONS = {"BUY", "SELL", "ADD", "TRIM"}
VALID_THESIS = {"YES", "NO", "NEEDS_REVIEW"}


@contextmanager
def _conn(path: str = SQLITE_PATH):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def log_trade(
    symbol: str,
    action: str,
    quantity: float | None = None,
    price: float | None = None,
    trigger_reason: str | None = None,
    emotion: str | None = None,
    thesis_still_valid: str | None = None,
    notes: str | None = None,
    path: str = SQLITE_PATH,
) -> int:
    if action not in VALID_ACTIONS:
        raise ValueError(f"action must be one of {VALID_ACTIONS}")
    if thesis_still_valid and thesis_still_valid not in VALID_THESIS:
        raise ValueError(f"thesis_still_valid must be one of {VALID_THESIS}")

    sql = """
        INSERT INTO trade_journal
            (timestamp, symbol, action, quantity, price,
             trigger_reason, emotion, thesis_still_valid, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    ts = datetime.now(UTC).isoformat()
    with _conn(path) as con:
        cur = con.execute(sql, (ts, symbol, action, quantity, price,
                                trigger_reason, emotion, thesis_still_valid, notes))
        entry_id = cur.lastrowid
    logger.info("Logged %s %s id=%d", action, symbol, entry_id)
    return entry_id


def get_recent(n: int = 20, path: str = SQLITE_PATH) -> pd.DataFrame:
    with _conn(path) as con:
        rows = con.execute(
            "SELECT * FROM trade_journal ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


def fill_30d_review(entry_id: int, review_text: str, path: str = SQLITE_PATH):
    with _conn(path) as con:
        con.execute(
            "UPDATE trade_journal SET review_30d = ? WHERE id = ?",
            (review_text, entry_id),
        )
    logger.info("30-day review filled for entry id=%d", entry_id)


def get_all(path: str = SQLITE_PATH) -> pd.DataFrame:
    with _conn(path) as con:
        rows = con.execute("SELECT * FROM trade_journal ORDER BY id DESC").fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])
