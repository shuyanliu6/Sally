"""
AI-infra-specific sector signals (Month 2 §3.2 – §3.4).

These signals capture the AI compute / cloud / API thesis specifically.
They are scored independently and contribute to the sector composite alongside
the generic SOX/SPX signal.

Data lives in SQLite (manual-entry friendly, persists across runs):
    tsmc_revenue       — Signal B (monthly)
    capex_surprise     — Signal C (quarterly)
    api_pricing        — Signal D (event-driven)

When data is missing or stale, signals return 0 (neutral) so the composite
still works on day one.
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date

import pandas as pd

from config.settings import SQLITE_PATH

logger = logging.getLogger(__name__)


# ── Schema ────────────────────────────────────────────────────────────────────

CREATE_TSMC_REVENUE = """
CREATE TABLE IF NOT EXISTS tsmc_revenue (
    month TEXT PRIMARY KEY,
    revenue_twd_bn REAL,
    yoy_growth REAL,
    ma3_yoy REAL,
    signal INT
);
"""

CREATE_CAPEX_SURPRISE = """
CREATE TABLE IF NOT EXISTS capex_surprise (
    quarter TEXT,
    company TEXT,
    actual_capex_bn REAL,
    consensus_capex_bn REAL,
    surprise_pct REAL,
    PRIMARY KEY (quarter, company)
);
"""

CREATE_API_PRICING = """
CREATE TABLE IF NOT EXISTS api_pricing (
    date TEXT,
    provider TEXT,
    model TEXT,
    price_per_m_input REAL,
    price_per_m_output REAL,
    PRIMARY KEY (date, provider, model)
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


def init_ai_infra_db(path: str = SQLITE_PATH):
    """Create the three SQLite tables. Idempotent."""
    with _conn(path) as con:
        con.execute(CREATE_TSMC_REVENUE)
        con.execute(CREATE_CAPEX_SURPRISE)
        con.execute(CREATE_API_PRICING)
    logger.info("AI-infra SQLite tables initialised at %s", path)


# ── Signal B: TSMC monthly revenue ────────────────────────────────────────────

def add_tsmc_revenue(
    month: str,           # "2026-01"
    revenue_twd_bn: float,
    path: str = SQLITE_PATH,
) -> dict:
    """Add a month of TSMC revenue. Recomputes YoY growth and 3M MA YoY,
    then derives Signal B per spec §3.2.

    Returns the inserted row (with derived fields).
    """
    init_ai_infra_db(path)
    with _conn(path) as con:
        # Find same month one year ago for YoY growth
        prior_year = _shift_month(month, -12)
        prev = con.execute(
            "SELECT revenue_twd_bn FROM tsmc_revenue WHERE month = ?",
            (prior_year,),
        ).fetchone()
        yoy = ((revenue_twd_bn - prev["revenue_twd_bn"]) / prev["revenue_twd_bn"] * 100
               if prev else None)

        # 3-month MA YoY
        recent = con.execute(
            "SELECT yoy_growth FROM tsmc_revenue WHERE month < ? "
            "ORDER BY month DESC LIMIT 2", (month,),
        ).fetchall()
        prev_yoys = [r["yoy_growth"] for r in recent if r["yoy_growth"] is not None]
        if yoy is not None:
            ma3 = (yoy + sum(prev_yoys)) / (1 + len(prev_yoys))
        else:
            ma3 = None

        # Score per spec §3.2
        signal = _score_tsmc(ma3, prev_ma3=_prev_ma3_yoy(con, month))

        con.execute(
            "INSERT OR REPLACE INTO tsmc_revenue "
            "(month, revenue_twd_bn, yoy_growth, ma3_yoy, signal) VALUES (?, ?, ?, ?, ?)",
            (month, revenue_twd_bn, yoy, ma3, signal),
        )

    logger.info("TSMC %s: rev=%.2fB TWD, YoY=%s, 3M MA YoY=%s → signal=%+d",
                month, revenue_twd_bn,
                f"{yoy:.1f}%" if yoy is not None else "n/a",
                f"{ma3:.1f}%" if ma3 is not None else "n/a",
                signal)
    return {"month": month, "revenue_twd_bn": revenue_twd_bn, "yoy_growth": yoy,
            "ma3_yoy": ma3, "signal": signal}


def _shift_month(yyyy_mm: str, delta_months: int) -> str:
    y, m = map(int, yyyy_mm.split("-"))
    total = y * 12 + (m - 1) + delta_months
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _prev_ma3_yoy(con, month: str) -> float | None:
    row = con.execute(
        "SELECT ma3_yoy FROM tsmc_revenue WHERE month < ? ORDER BY month DESC LIMIT 1",
        (month,),
    ).fetchone()
    return row["ma3_yoy"] if row and row["ma3_yoy"] is not None else None


def _score_tsmc(ma3_yoy: float | None, prev_ma3: float | None) -> int:
    """Score per spec §3.2 table."""
    if ma3_yoy is None:
        return 0
    accelerating = prev_ma3 is not None and ma3_yoy > prev_ma3
    if ma3_yoy > 30 and accelerating:
        return 2
    if ma3_yoy > 15:
        return 1
    if ma3_yoy >= 0:
        return 0
    # Negative — check for sustained downturn (2 consecutive negative months)
    return -2 if (prev_ma3 is not None and prev_ma3 < 0) else -1


def latest_tsmc_signal(path: str = SQLITE_PATH) -> int:
    """Return the most recent stored TSMC signal (sticky between updates)."""
    try:
        with _conn(path) as con:
            row = con.execute(
                "SELECT signal FROM tsmc_revenue ORDER BY month DESC LIMIT 1"
            ).fetchone()
            return int(row["signal"]) if row and row["signal"] is not None else 0
    except sqlite3.OperationalError:
        return 0  # table doesn't exist yet


# ── Signal C: Hyperscaler Capex Surprise ──────────────────────────────────────

CAPEX_TRACKED_COMPANIES = ("META", "MSFT", "GOOGL", "AMZN")


def add_capex_surprise(
    quarter: str,                # "2026-Q1"
    company: str,                # "META" | "MSFT" | "GOOGL" | "AMZN"
    actual_capex_bn: float,
    consensus_capex_bn: float,
    path: str = SQLITE_PATH,
):
    """Record one company's capex surprise. Score is derived later from the
    full quarter's average across all reporting companies."""
    init_ai_infra_db(path)
    surprise_pct = ((actual_capex_bn - consensus_capex_bn) / consensus_capex_bn
                    if consensus_capex_bn else 0.0)
    with _conn(path) as con:
        con.execute(
            "INSERT OR REPLACE INTO capex_surprise "
            "(quarter, company, actual_capex_bn, consensus_capex_bn, surprise_pct) "
            "VALUES (?, ?, ?, ?, ?)",
            (quarter, company, actual_capex_bn, consensus_capex_bn, surprise_pct),
        )
    logger.info("Capex %s %s: actual=%.1fB vs consensus=%.1fB → %+.1f%%",
                quarter, company, actual_capex_bn, consensus_capex_bn, surprise_pct * 100)


def calc_capex_signal_for_quarter(quarter: str, path: str = SQLITE_PATH) -> int:
    """Compute Signal C for a specific quarter from stored data."""
    try:
        with _conn(path) as con:
            rows = con.execute(
                "SELECT surprise_pct FROM capex_surprise WHERE quarter = ?",
                (quarter,),
            ).fetchall()
    except sqlite3.OperationalError:
        return 0
    surprises = [r["surprise_pct"] for r in rows if r["surprise_pct"] is not None]
    if len(surprises) < 2:
        return 0  # insufficient — neutral
    avg = sum(surprises) / len(surprises)
    if avg > 0.10:  return 2
    if avg > 0.0:   return 1
    if avg > -0.10: return -1
    return -2


def latest_capex_signal(path: str = SQLITE_PATH) -> int:
    """Get the latest quarter that has at least 2 companies reporting,
    return Signal C for that quarter (sticky between quarters)."""
    try:
        with _conn(path) as con:
            rows = con.execute(
                "SELECT quarter, count(*) AS n FROM capex_surprise "
                "GROUP BY quarter HAVING n >= 2 ORDER BY quarter DESC LIMIT 1"
            ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if not rows:
        return 0
    return calc_capex_signal_for_quarter(rows["quarter"], path)


# ── Signal D: AI API pricing trend ────────────────────────────────────────────

def add_api_pricing(
    pricing_date: str,           # YYYY-MM-DD
    provider: str,               # "OpenAI" | "Anthropic" | "Google"
    model: str,
    price_per_m_input: float,
    price_per_m_output: float,
    path: str = SQLITE_PATH,
):
    """Record a pricing observation for one provider/model on one date."""
    init_ai_infra_db(path)
    with _conn(path) as con:
        con.execute(
            "INSERT OR REPLACE INTO api_pricing "
            "(date, provider, model, price_per_m_input, price_per_m_output) "
            "VALUES (?, ?, ?, ?, ?)",
            (pricing_date, provider, model, price_per_m_input, price_per_m_output),
        )
    logger.info("API pricing %s %s/%s: in=%.2f out=%.2f $/MTok",
                pricing_date, provider, model, price_per_m_input, price_per_m_output)


def latest_api_pricing_signal(path: str = SQLITE_PATH) -> int:
    """Score Signal D from the trajectory of average input pricing across
    providers over the last 90 days.

    Per spec §3.4:
        +1  prices rising or stable, no cuts in 3 months
         0  dropping < 30% per quarter (normal efficiency gains)
        -1  dropping > 30% per quarter
        -2  dropping > 50% per quarter (DeepSeek-scale shock)
    """
    try:
        with _conn(path) as con:
            df = pd.read_sql_query(
                "SELECT date, provider, price_per_m_input "
                "FROM api_pricing ORDER BY date",
                con,
            )
    except (sqlite3.OperationalError, pd.errors.DatabaseError):
        return 0
    if df.empty:
        return 0

    # Average input price across providers per date
    df["date"] = pd.to_datetime(df["date"])
    daily_avg = df.groupby("date")["price_per_m_input"].mean().sort_index()

    if len(daily_avg) < 2:
        return 1   # only 1 observation — assume stable

    # Compare latest to ~90 days ago (or earliest if shorter history)
    cutoff = daily_avg.index[-1] - pd.Timedelta(days=90)
    quarter_ago_idx = daily_avg.index[daily_avg.index <= cutoff]
    if quarter_ago_idx.empty:
        baseline = daily_avg.iloc[0]
    else:
        baseline = daily_avg.loc[quarter_ago_idx[-1]]

    latest = daily_avg.iloc[-1]
    pct_change = (latest - baseline) / baseline if baseline else 0

    if pct_change >= -0.001:   # rising or essentially flat
        return 1
    if pct_change > -0.30:     # < 30% drop
        return 0
    if pct_change > -0.50:     # 30–50% drop
        return -1
    return -2                   # > 50% drop
