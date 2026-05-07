"""
Tests for holiday-aware prev_trading_day (D2 fix).
No network, no DB.
"""

import pytest
from datetime import date

from quantamental.data.ingest.polygon_client import prev_trading_day


# Skip the whole module if pandas_market_calendars isn't installed —
# in that case the function falls back to weekday-only and these specific
# holiday tests would (correctly) fail.
mcal = pytest.importorskip("pandas_market_calendars")


class TestHolidayCalendar:
    def test_christmas_skipped(self):
        # Dec 26 2024 was Thursday — Dec 25 was Christmas (closed)
        # Expected: Dec 24 2024 (Tuesday)
        assert prev_trading_day(date(2024, 12, 26)) == date(2024, 12, 24)

    def test_july_4_skipped(self):
        # July 5 2024 was Friday — July 4 closed
        # Expected: July 3 2024 (Wednesday)
        assert prev_trading_day(date(2024, 7, 5)) == date(2024, 7, 3)

    def test_thanksgiving_skipped(self):
        # Nov 29 2024 (Friday after Thanksgiving) — Nov 28 was Thanksgiving (closed)
        # Expected: Nov 27 2024 (Wednesday)
        assert prev_trading_day(date(2024, 11, 29)) == date(2024, 11, 27)

    def test_mlk_day_skipped(self):
        # Jan 21 2025 was Tuesday — Jan 20 (Mon) was MLK Day (closed)
        # Expected: Jan 17 2025 (Friday)
        assert prev_trading_day(date(2025, 1, 21)) == date(2025, 1, 17)

    def test_new_years_day_skipped(self):
        # Jan 2 2025 was Thursday — Jan 1 was New Year's Day (closed)
        # Expected: Dec 31 2024 (Tuesday)
        assert prev_trading_day(date(2025, 1, 2)) == date(2024, 12, 31)

    def test_normal_weekday(self):
        # Wed Apr 24 2024 — should return Tue Apr 23 2024 (no holiday between)
        assert prev_trading_day(date(2024, 4, 24)) == date(2024, 4, 23)

    def test_monday_skips_weekend(self):
        # Mon Apr 22 2024 — should return Fri Apr 19 2024
        assert prev_trading_day(date(2024, 4, 22)) == date(2024, 4, 19)

    def test_returns_date_type(self):
        # Make sure we always return a `date`, not a `Timestamp`
        result = prev_trading_day(date(2024, 4, 24))
        assert isinstance(result, date)
        assert not hasattr(result, "tzinfo")  # date has no tzinfo, datetime does
