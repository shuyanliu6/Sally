"""Earnings event storage for PEAD signals."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime

import pandas as pd

from quantamental.config.settings import SQLITE_PATH


CREATE_EARNINGS_EVENTS = """
CREATE TABLE IF NOT EXISTS earnings_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    report_date TEXT NOT NULL,
    fiscal_period TEXT,
    eps_actual REAL,
    eps_estimate REAL,
    surprise_pct REAL NOT NULL,
    source TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(symbol, report_date)
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


def init_earnings_events(path: str = SQLITE_PATH) -> None:
    with _conn(path) as con:
        con.execute(CREATE_EARNINGS_EVENTS)


def _compute_surprise_pct(eps_actual: float | None, eps_estimate: float | None) -> float | None:
    if eps_actual is None or eps_estimate is None or eps_estimate == 0:
        return None
    return (float(eps_actual) - float(eps_estimate)) / abs(float(eps_estimate)) * 100.0


def log_earnings_event(
    symbol: str,
    report_date: str | date,
    surprise_pct: float | None = None,
    eps_actual: float | None = None,
    eps_estimate: float | None = None,
    fiscal_period: str | None = None,
    source: str | None = None,
    notes: str | None = None,
    path: str = SQLITE_PATH,
) -> int:
    """Insert or update one earnings event.

    ``surprise_pct`` is stored in percent points, e.g. ``12.5`` for a 12.5%
    beat. If omitted, it is derived from ``eps_actual`` and ``eps_estimate``.
    """
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("symbol is required")

    report_date_text = date.fromisoformat(str(report_date)).isoformat()
    if surprise_pct is None:
        surprise_pct = _compute_surprise_pct(eps_actual, eps_estimate)
    if surprise_pct is None:
        raise ValueError("provide surprise_pct or both eps_actual and eps_estimate")

    init_earnings_events(path)
    sql = """
        INSERT INTO earnings_events
            (symbol, report_date, fiscal_period, eps_actual, eps_estimate,
             surprise_pct, source, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, report_date) DO UPDATE SET
            fiscal_period = excluded.fiscal_period,
            eps_actual = excluded.eps_actual,
            eps_estimate = excluded.eps_estimate,
            surprise_pct = excluded.surprise_pct,
            source = excluded.source,
            notes = excluded.notes,
            created_at = excluded.created_at
    """
    created_at = datetime.now(UTC).isoformat()
    with _conn(path) as con:
        cur = con.execute(
            sql,
            (
                symbol,
                report_date_text,
                fiscal_period,
                eps_actual,
                eps_estimate,
                float(surprise_pct),
                source,
                notes,
                created_at,
            ),
        )
        row = con.execute(
            "SELECT id FROM earnings_events WHERE symbol = ? AND report_date = ?",
            (symbol, report_date_text),
        ).fetchone()
    return int(row["id"] if row else cur.lastrowid)


def load_earnings_events(
    symbols: list[str] | None = None,
    start: str | date | pd.Timestamp | None = None,
    end: str | date | pd.Timestamp | None = None,
    path: str = SQLITE_PATH,
) -> pd.DataFrame:
    init_earnings_events(path)
    clauses = []
    params: list[object] = []
    if symbols:
        clean = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
        if clean:
            placeholders = ", ".join("?" for _ in clean)
            clauses.append(f"symbol IN ({placeholders})")
            params.extend(clean)
    if start is not None:
        clauses.append("report_date >= ?")
        params.append(pd.Timestamp(start).date().isoformat())
    if end is not None:
        clauses.append("report_date <= ?")
        params.append(pd.Timestamp(end).date().isoformat())

    sql = "SELECT * FROM earnings_events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY report_date, symbol"

    with _conn(path) as con:
        rows = con.execute(sql, params).fetchall()
    if not rows:
        return pd.DataFrame(
            columns=[
                "id",
                "symbol",
                "report_date",
                "fiscal_period",
                "eps_actual",
                "eps_estimate",
                "surprise_pct",
                "source",
                "notes",
                "created_at",
            ]
        )
    return pd.DataFrame([dict(r) for r in rows])
