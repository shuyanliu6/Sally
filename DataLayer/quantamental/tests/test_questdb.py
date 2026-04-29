"""
Integration tests for QuestDB read/write.
Requires: docker compose up -d  (QuestDB on localhost:8812)

Run with:  pytest tests/test_questdb.py -v
Skip with: pytest tests/ --ignore=tests/test_questdb.py
"""

import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pandas as pd
from datetime import datetime, timezone


# Skip entire module if QuestDB is unreachable
def _questdb_available():
    try:
        from data.ingest.questdb_writer import get_connection
        conn = get_connection()
        conn.close()
        return True
    except Exception:
        return False

pytestmark = pytest.mark.skipif(
    not _questdb_available(),
    reason="QuestDB not reachable on localhost:8812 — run: docker compose up -d"
)


@pytest.fixture(scope="module")
def writer():
    from data.ingest import questdb_writer
    questdb_writer.init_schema()
    return questdb_writer


class TestQuestDBWriter:
    def test_init_schema_idempotent(self, writer):
        # Running twice should not raise
        writer.init_schema()

    def test_write_and_query_ohlcv(self, writer):
        ts = datetime(2020, 1, 2, 21, 0, 0, tzinfo=timezone.utc)
        df = pd.DataFrame([{
            "symbol": "_TEST",
            "ts": ts,
            "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0,
            "volume": 1000000, "vwap": 102.0, "num_trades": 5000,
        }])
        writer.write_ohlcv(df)
        result = writer.query("SELECT * FROM daily_ohlcv WHERE symbol = '_TEST'")
        assert len(result) >= 1
        row = result[result["symbol"] == "_TEST"].iloc[-1]
        assert row["close"] == pytest.approx(103.0)

    def test_write_and_query_macro(self, writer):
        ts = datetime(2020, 1, 3, tzinfo=timezone.utc)  # unique ts to avoid dedup
        df = pd.DataFrame([{
            "ts": ts,
            "value": 4.25,
            "ma_20": 4.20,
            "ma_60": 4.30,
            "signal": 1,
        }])
        writer.write_macro(df, "_test_yield")
        # QuestDB has a commit lag (QDB_CAIRO_COMMIT_LAG=1000ms); wait for visibility
        time.sleep(1.5)
        result = writer.query(
            "SELECT * FROM macro_indicators WHERE indicator = '_test_yield'"
        )
        assert len(result) >= 1
        assert result.iloc[-1]["value"] == pytest.approx(4.25)

    def test_write_and_query_signals(self, writer):
        row = {
            "ts": datetime(2020, 1, 2, tzinfo=timezone.utc),
            "yield_10y_signal": 2,
            "vix_signal": 1,
            "fed_bs_signal": 1,
            "credit_spread_signal": 1,
            "composite_score": 5,
            "regime": "RISK_ON",
        }
        writer.write_signals(row)
        result = writer.query(
            "SELECT * FROM regime_signals WHERE regime = 'RISK_ON' ORDER BY ts DESC LIMIT 1"
        )
        assert len(result) >= 1
        assert result.iloc[0]["composite_score"] == 5

    def test_query_returns_dataframe(self, writer):
        df = writer.query("SELECT 1 AS n")
        assert isinstance(df, pd.DataFrame)
        assert df.iloc[0]["n"] == 1


class TestIncrementalWrites:
    """D1 fix verification: writing the same data twice should not duplicate rows."""

    def test_write_macro_dedups(self, writer):
        # Use a unique indicator name and unique ts to avoid colliding with other tests
        indicator = "_dedup_macro"
        ts = datetime(2019, 5, 15, tzinfo=timezone.utc)
        df = pd.DataFrame([{
            "ts": ts, "value": 1.23, "ma_20": 1.20, "ma_60": 1.15, "signal": 0,
        }])

        writer.write_macro(df, indicator)
        time.sleep(1.5)  # commit lag

        # Snapshot the count after first write
        n1 = int(writer.query(
            f"SELECT count() AS n FROM macro_indicators WHERE indicator = '{indicator}'"
        )["n"].iloc[0])
        assert n1 >= 1

        # Write the same row again — should be skipped
        writer.write_macro(df, indicator)
        time.sleep(1.5)

        n2 = int(writer.query(
            f"SELECT count() AS n FROM macro_indicators WHERE indicator = '{indicator}'"
        )["n"].iloc[0])
        assert n2 == n1, f"write_macro duplicated: was {n1}, now {n2}"

    def test_write_macro_inserts_only_new(self, writer):
        indicator = "_dedup_incremental"
        # Two-row history
        df_initial = pd.DataFrame([
            {"ts": datetime(2019, 6, 1, tzinfo=timezone.utc),  "value": 1.0,
             "ma_20": 1.0, "ma_60": 1.0, "signal": 0},
            {"ts": datetime(2019, 6, 2, tzinfo=timezone.utc),  "value": 1.1,
             "ma_20": 1.0, "ma_60": 1.0, "signal": 1},
        ])
        writer.write_macro(df_initial, indicator)
        time.sleep(1.5)

        # Now "fetch" full history (including a new day) and write again
        df_full = pd.DataFrame([
            {"ts": datetime(2019, 6, 1, tzinfo=timezone.utc),  "value": 1.0,
             "ma_20": 1.0, "ma_60": 1.0, "signal": 0},
            {"ts": datetime(2019, 6, 2, tzinfo=timezone.utc),  "value": 1.1,
             "ma_20": 1.0, "ma_60": 1.0, "signal": 1},
            {"ts": datetime(2019, 6, 3, tzinfo=timezone.utc),  "value": 1.2,
             "ma_20": 1.05, "ma_60": 1.0, "signal": 1},  # only new row
        ])
        writer.write_macro(df_full, indicator)
        time.sleep(1.5)

        n = int(writer.query(
            f"SELECT count() AS n FROM macro_indicators WHERE indicator = '{indicator}'"
        )["n"].iloc[0])
        assert n == 3, f"expected 3 distinct rows after incremental write, got {n}"

    def test_write_signals_dedups_same_day(self, writer):
        # Use a far-past date so it doesn't collide with any real signal data
        target_ts = datetime(2018, 3, 14, 12, 0, 0, tzinfo=timezone.utc)
        row = {
            "ts": target_ts,
            "yield_10y_signal": 1, "vix_signal": 1,
            "fed_bs_signal": 1, "credit_spread_signal": 1,
            "composite_score": 4, "regime": "MODERATE_ON",
        }
        writer.write_signals(row)
        time.sleep(1.5)
        n1 = int(writer.query(
            "SELECT count() AS n FROM regime_signals "
            "WHERE ts >= '2018-03-14' AND ts < '2018-03-15'"
        )["n"].iloc[0])
        assert n1 == 1

        # Same day, different time — should still be deduped
        row["ts"] = datetime(2018, 3, 14, 17, 0, 0, tzinfo=timezone.utc)
        writer.write_signals(row)
        time.sleep(1.5)
        n2 = int(writer.query(
            "SELECT count() AS n FROM regime_signals "
            "WHERE ts >= '2018-03-14' AND ts < '2018-03-15'"
        )["n"].iloc[0])
        assert n2 == 1, f"write_signals duplicated same-day row: was {n1}, now {n2}"
