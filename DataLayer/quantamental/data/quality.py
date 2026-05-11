"""Persistent data-quality audit events."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from quantamental.config.settings import SQLITE_PATH


@dataclass(frozen=True)
class DataQualityEvent:
    run_id: str
    asof_date: str
    component: str
    symbol: str | None
    severity: str
    check_name: str
    status: str
    observed: str | None = None
    expected: str | None = None
    detail: str = ""
    fix_hint: str | None = None
    created_at: str | None = None


DDL = """
CREATE TABLE IF NOT EXISTS data_quality_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    component TEXT NOT NULL,
    symbol TEXT,
    severity TEXT NOT NULL,
    check_name TEXT NOT NULL,
    status TEXT NOT NULL,
    observed TEXT,
    expected TEXT,
    detail TEXT NOT NULL,
    fix_hint TEXT,
    created_at TEXT NOT NULL
);
"""


def _connect(path: str | Path = SQLITE_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def init_data_quality_events(path: str | Path = SQLITE_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as con:
        con.execute(DDL)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_data_quality_run "
            "ON data_quality_events(run_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_data_quality_asof "
            "ON data_quality_events(asof_date, component, symbol)"
        )


def record_data_quality_events(
    events: Iterable[DataQualityEvent],
    path: str | Path = SQLITE_PATH,
) -> int:
    """Persist audit events and return the number of inserted rows."""
    rows = []
    now = datetime.now(UTC).isoformat()
    for event in events:
        payload = asdict(event)
        payload["created_at"] = payload["created_at"] or now
        rows.append(payload)
    if not rows:
        return 0

    init_data_quality_events(path)
    with _connect(path) as con:
        con.executemany(
            """
            INSERT INTO data_quality_events
                (run_id, asof_date, component, symbol, severity, check_name,
                 status, observed, expected, detail, fix_hint, created_at)
            VALUES
                (:run_id, :asof_date, :component, :symbol, :severity,
                 :check_name, :status, :observed, :expected, :detail,
                 :fix_hint, :created_at)
            """,
            rows,
        )
    return len(rows)


def load_data_quality_events(
    *,
    run_id: str | None = None,
    asof_date: str | date | None = None,
    limit: int = 200,
    path: str | Path = SQLITE_PATH,
) -> pd.DataFrame:
    """Load recent audit events for dashboard/reporting use."""
    init_data_quality_events(path)
    clauses = []
    params: dict[str, object] = {"limit": int(limit)}
    if run_id:
        clauses.append("run_id = :run_id")
        params["run_id"] = run_id
    if asof_date:
        clauses.append("asof_date = :asof_date")
        params["asof_date"] = str(asof_date)

    sql = "SELECT * FROM data_quality_events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC, id DESC LIMIT :limit"
    with _connect(path) as con:
        rows = con.execute(sql, params).fetchall()
    return pd.DataFrame([dict(row) for row in rows])
