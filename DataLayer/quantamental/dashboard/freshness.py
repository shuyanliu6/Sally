"""Dashboard data freshness guardrails."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd


MARKET_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class FreshnessCheck:
    component: str
    status: str
    latest_date: date | None
    expected_date: date | None
    lag_days: int | None
    detail: str
    fix: str


def previous_weekday(value: date) -> date:
    day = value - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def expected_market_date(now: datetime | None = None) -> date:
    """Latest US trading date expected to be available for daily OHLCV.

    This deliberately uses a simple weekday calendar. It is conservative enough
    for dashboard trust gating and avoids pulling in an exchange-calendar
    dependency for the UI path.
    """
    current = now or datetime.now(MARKET_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MARKET_TZ)
    current = current.astimezone(MARKET_TZ)
    current_date = current.date()

    if current_date.weekday() >= 5:
        while current_date.weekday() >= 5:
            current_date -= timedelta(days=1)
        return current_date

    if current.time() < time(18, 0):
        return previous_weekday(current_date)
    return current_date


def trading_day_lag(latest: date | None, expected: date | None) -> int | None:
    if latest is None or expected is None:
        return None
    if latest >= expected:
        return 0
    lag = 0
    day = latest
    while day < expected:
        day += timedelta(days=1)
        if day.weekday() < 5:
            lag += 1
    return lag


def calendar_day_lag(latest: date | None, expected: date | None) -> int | None:
    if latest is None or expected is None:
        return None
    return max(0, (expected - latest).days)


def status_from_lag(lag: int | None, *, warn_after: int = 0, fail_after: int = 1) -> str:
    if lag is None:
        return "FAIL"
    if lag > fail_after:
        return "FAIL"
    if lag > warn_after:
        return "WARN"
    return "OK"


def parse_date(value) -> date | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError):
        return None


def _day_window(day: date) -> tuple[str, str]:
    return day.isoformat(), (day + timedelta(days=1)).isoformat()


def _count_symbols_on_date(query_fn, table: str, symbols: list[str], latest: date | None) -> int:
    if not symbols or latest is None:
        return 0
    from quantamental.data.ingest.questdb_connection import symbol_list_clause

    clause, params = symbol_list_clause(symbols)
    start, end = _day_window(latest)
    params = {**params, "start": start, "end": end}
    df = query_fn(
        f"""
        SELECT count_distinct(symbol) AS symbols
        FROM {table}
        WHERE symbol IN ({clause}) AND ts >= :start AND ts < :end
        """,
        params,
    )
    if df.empty:
        return 0
    return int(df["symbols"].iloc[0] or 0)


def _latest_for_symbols(query_fn, table: str, symbols: list[str]) -> date | None:
    from quantamental.data.ingest.questdb_connection import symbol_list_clause

    clause, params = symbol_list_clause(symbols)
    df = query_fn(f"SELECT max(ts) AS latest FROM {table} WHERE symbol IN ({clause})", params)
    if df.empty:
        return None
    return parse_date(df["latest"].iloc[0])


def _latest_table_date(query_fn, table: str) -> date | None:
    df = query_fn(f"SELECT max(ts) AS latest FROM {table}")
    if df.empty:
        return None
    return parse_date(df["latest"].iloc[0])


def _alpha_asof(alpha_ranks: pd.DataFrame | None) -> date | None:
    if alpha_ranks is None or alpha_ranks.empty or "asof_date" not in alpha_ranks:
        return None
    return parse_date(alpha_ranks["asof_date"].iloc[0])


def _check_market_data(query_fn, symbols: list[str], expected: date) -> FreshnessCheck:
    try:
        latest = _latest_for_symbols(query_fn, "daily_ohlcv", symbols)
        lag = trading_day_lag(latest, expected)
        status = status_from_lag(lag, warn_after=0, fail_after=1)
        coverage = _count_symbols_on_date(query_fn, "daily_ohlcv", symbols, latest)
        coverage_ok = coverage >= max(1, int(len(symbols) * 0.9)) if symbols else False
        if not coverage_ok:
            status = "FAIL"
        return FreshnessCheck(
            component="OHLCV",
            status=status,
            latest_date=latest,
            expected_date=expected,
            lag_days=lag,
            detail=f"{coverage}/{len(symbols)} candidates on latest date",
            fix="python scripts/daily_pipeline.py --step fetch_market --force",
        )
    except Exception as exc:
        return FreshnessCheck("OHLCV", "FAIL", None, expected, None, f"query failed: {exc}", "docker compose up -d")


def _check_symbol_signals(
    query_fn,
    symbols: list[str],
    expected: date,
) -> FreshnessCheck:
    try:
        latest = _latest_for_symbols(query_fn, "stock_signals", symbols)
        lag = calendar_day_lag(latest, expected)
        status = status_from_lag(lag, warn_after=0, fail_after=1)
        coverage = _count_symbols_on_date(query_fn, "stock_signals", symbols, latest)
        coverage_ok = coverage >= max(1, int(len(symbols) * 0.9)) if symbols else False
        if not coverage_ok:
            status = "FAIL"
        return FreshnessCheck(
            component="Stock Signals",
            status=status,
            latest_date=latest,
            expected_date=expected,
            lag_days=lag,
            detail=f"{coverage}/{len(symbols)} candidates scored",
            fix="python scripts/daily_pipeline.py --step calc_stock_signals --force",
        )
    except Exception as exc:
        return FreshnessCheck("Stock Signals", "FAIL", None, expected, None, f"query failed: {exc}", "docker compose up -d")


def _check_daily_signal(query_fn, table: str, component: str, expected: date, fix: str) -> FreshnessCheck:
    try:
        latest = _latest_table_date(query_fn, table)
        lag = calendar_day_lag(latest, expected)
        status = status_from_lag(lag, warn_after=0, fail_after=1)
        return FreshnessCheck(
            component=component,
            status=status,
            latest_date=latest,
            expected_date=expected,
            lag_days=lag,
            detail="latest row found" if latest else "no rows found",
            fix=fix,
        )
    except Exception as exc:
        return FreshnessCheck(component, "FAIL", None, expected, None, f"query failed: {exc}", "docker compose up -d")


def _check_alpha_ranks(alpha_ranks: pd.DataFrame | None, expected: date) -> FreshnessCheck:
    latest = _alpha_asof(alpha_ranks)
    lag = calendar_day_lag(latest, expected)
    status = status_from_lag(lag, warn_after=0, fail_after=1)
    rows = 0 if alpha_ranks is None else len(alpha_ranks)
    if rows == 0:
        status = "FAIL"
    return FreshnessCheck(
        component="Alpha Ranks",
        status=status,
        latest_date=latest,
        expected_date=expected,
        lag_days=lag,
        detail=f"{rows} ranked rows",
        fix="python scripts/run_alpha.py --asof YYYY-MM-DD",
    )


def build_freshness_report(
    *,
    query_fn=None,
    alpha_ranks: pd.DataFrame | None = None,
    symbols: list[str] | None = None,
    now: datetime | None = None,
) -> dict:
    """Return dashboard-friendly freshness status for live decision trust."""
    if query_fn is None:
        from quantamental.data.ingest.questdb_writer import query as query_fn
    if symbols is None:
        from quantamental.config.universe import load_candidate_list

        symbols = load_candidate_list()

    market_expected = expected_market_date(now)
    if now is None:
        signal_now = datetime.now(ZoneInfo("UTC"))
    elif now.tzinfo is None:
        signal_now = now.replace(tzinfo=ZoneInfo("UTC"))
    else:
        signal_now = now.astimezone(ZoneInfo("UTC"))
    signal_expected = signal_now.date()

    checks = [
        _check_market_data(query_fn, symbols, market_expected),
        _check_symbol_signals(query_fn, symbols, signal_expected),
        _check_daily_signal(
            query_fn,
            "regime_signals",
            "Macro Regime",
            signal_expected,
            "python scripts/daily_pipeline.py --step calc_signals --force",
        ),
        _check_daily_signal(
            query_fn,
            "sector_signals",
            "Sector Signals",
            signal_expected,
            "python scripts/daily_pipeline.py --step calc_sector_signals --force",
        ),
    ]

    latest_signal_dates = [c.latest_date for c in checks[1:] if c.latest_date is not None]
    alpha_expected = max(latest_signal_dates) if latest_signal_dates else signal_expected
    checks.append(_check_alpha_ranks(alpha_ranks, alpha_expected))

    if any(c.status == "FAIL" for c in checks):
        overall = "BLOCKED"
    elif any(c.status == "WARN" for c in checks):
        overall = "LIMITED"
    else:
        overall = "TRUSTED"

    return {
        "status": overall,
        "trusted": overall == "TRUSTED",
        "asof": datetime.now(ZoneInfo("UTC")).isoformat(),
        "checks": [asdict(c) for c in checks],
    }
