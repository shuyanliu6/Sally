from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from quantamental.dashboard.freshness import (
    build_freshness_report,
    dashboard_clock,
    expected_market_date,
    trading_day_lag,
)
from quantamental.scripts.run_alpha import _parse_date


def test_expected_market_date_waits_until_after_close_buffer():
    ny = ZoneInfo("America/New_York")

    before_buffer = datetime(2026, 5, 7, 17, 30, tzinfo=ny)
    after_buffer = datetime(2026, 5, 7, 18, 30, tzinfo=ny)

    assert expected_market_date(before_buffer).isoformat() == "2026-05-06"
    assert expected_market_date(after_buffer).isoformat() == "2026-05-07"


def test_dashboard_clock_shows_china_new_york_and_used_market_date():
    clock = dashboard_clock(datetime(2026, 5, 9, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")))

    assert clock["china_now"] == "2026-05-09 09:30 CST"
    assert clock["us_now"] == "2026-05-08 21:30 EDT"
    assert clock["us_market_date"] == "2026-05-08"


def test_trading_day_lag_skips_weekends():
    latest = pd.Timestamp("2026-05-01").date()
    expected = pd.Timestamp("2026-05-04").date()

    assert trading_day_lag(latest, expected) == 1


def test_build_freshness_report_blocks_stale_ohlcv():
    alpha_ranks = pd.DataFrame(
        {
            "symbol": ["NVDA", "AMD"],
            "asof_date": ["2026-05-07", "2026-05-07"],
        }
    )

    def fake_query(sql, params=None):
        if "FROM daily_ohlcv" in sql and "max(ts)" in sql:
            return pd.DataFrame({"latest": [pd.Timestamp("2026-05-05")]})
        if "FROM daily_ohlcv" in sql and "count_distinct" in sql:
            return pd.DataFrame({"symbols": [2]})
        if "FROM stock_signals" in sql and "max(ts)" in sql:
            return pd.DataFrame({"latest": [pd.Timestamp("2026-05-07")]})
        if "FROM stock_signals" in sql and "count_distinct" in sql:
            return pd.DataFrame({"symbols": [2]})
        if "FROM regime_signals" in sql:
            return pd.DataFrame({"latest": [pd.Timestamp("2026-05-07")]})
        if "FROM sector_signals" in sql:
            return pd.DataFrame({"latest": [pd.Timestamp("2026-05-07")]})
        raise AssertionError(sql)

    report = build_freshness_report(
        query_fn=fake_query,
        alpha_ranks=alpha_ranks,
        symbols=["NVDA", "AMD"],
        now=datetime(2026, 5, 7, 18, 30, tzinfo=ZoneInfo("America/New_York")),
    )

    assert report["status"] == "BLOCKED"
    ohlcv = next(check for check in report["checks"] if check["component"] == "OHLCV")
    assert ohlcv["status"] == "FAIL"
    assert ohlcv["lag_days"] == 2


def test_build_freshness_report_trusted_when_all_inputs_current():
    alpha_ranks = pd.DataFrame(
        {
            "symbol": ["NVDA", "AMD"],
            "asof_date": ["2026-05-07", "2026-05-07"],
        }
    )

    def fake_query(sql, params=None):
        if "FROM daily_ohlcv" in sql and "max(ts)" in sql:
            return pd.DataFrame({"latest": [pd.Timestamp("2026-05-07")]})
        if "FROM daily_ohlcv" in sql and "count_distinct" in sql:
            return pd.DataFrame({"symbols": [2]})
        if "FROM stock_signals" in sql and "max(ts)" in sql:
            return pd.DataFrame({"latest": [pd.Timestamp("2026-05-07")]})
        if "FROM stock_signals" in sql and "count_distinct" in sql:
            return pd.DataFrame({"symbols": [2]})
        if "FROM regime_signals" in sql:
            return pd.DataFrame({"latest": [pd.Timestamp("2026-05-07")]})
        if "FROM sector_signals" in sql:
            return pd.DataFrame({"latest": [pd.Timestamp("2026-05-07")]})
        raise AssertionError(sql)

    report = build_freshness_report(
        query_fn=fake_query,
        alpha_ranks=alpha_ranks,
        symbols=["NVDA", "AMD"],
        now=datetime(2026, 5, 7, 18, 30, tzinfo=ZoneInfo("America/New_York")),
    )

    assert report["status"] == "TRUSTED"
    assert report["trusted"] is True


def test_china_morning_accepts_previous_us_market_alpha_date():
    alpha_ranks = pd.DataFrame(
        {
            "symbol": ["NVDA", "AMD"],
            "asof_date": ["2026-05-08", "2026-05-08"],
        }
    )

    def fake_query(sql, params=None):
        if "FROM daily_ohlcv" in sql and "max(ts)" in sql:
            return pd.DataFrame({"latest": [pd.Timestamp("2026-05-08 20:00:00")]})
        if "FROM daily_ohlcv" in sql and "count_distinct" in sql:
            return pd.DataFrame({"symbols": [2]})
        if "FROM stock_signals" in sql and "max(ts)" in sql:
            return pd.DataFrame({"latest": [pd.Timestamp("2026-05-09 01:00:00")]})
        if "FROM stock_signals" in sql and "count_distinct" in sql:
            return pd.DataFrame({"symbols": [2]})
        if "FROM regime_signals" in sql:
            return pd.DataFrame({"latest": [pd.Timestamp("2026-05-09 01:00:00")]})
        if "FROM sector_signals" in sql:
            return pd.DataFrame({"latest": [pd.Timestamp("2026-05-09 01:00:00")]})
        raise AssertionError(sql)

    report = build_freshness_report(
        query_fn=fake_query,
        alpha_ranks=alpha_ranks,
        symbols=["NVDA", "AMD"],
        now=datetime(2026, 5, 9, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert report["status"] == "TRUSTED"
    alpha = next(check for check in report["checks"] if check["component"] == "Alpha Ranks")
    assert alpha["expected_date"].isoformat() == "2026-05-08"
    assert alpha["latest_date"].isoformat() == "2026-05-08"


def test_run_alpha_default_asof_uses_expected_us_market_date(monkeypatch):
    import quantamental.dashboard.freshness as freshness

    monkeypatch.setattr(freshness, "expected_market_date", lambda: pd.Timestamp("2026-05-08").date())

    assert _parse_date(None).isoformat() == "2026-05-08"
