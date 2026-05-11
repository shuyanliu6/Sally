"""Hardening tests for packaging, query params, and migrations."""

import importlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from quantamental.data.ingest import questdb_connection as conn
from quantamental.data.ingest import questdb_schema as schema
from quantamental.data.quality import (
    DataQualityEvent,
    load_data_quality_events,
    record_data_quality_events,
)


class _FakeConnection:
    def __enter__(self):
        return "connection"

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeEngine:
    def connect(self):
        return _FakeConnection()


def test_query_accepts_params_and_literal_percent():
    with patch.object(conn, "_get_engine", return_value=_FakeEngine()), \
         patch.object(conn.pd, "read_sql_query", return_value=pd.DataFrame()) as read_sql:
        conn.query("SELECT 'hello%' AS s WHERE symbol = :symbol", {"symbol": "NVDA"})

    sql_arg, connection_arg = read_sql.call_args.args[:2]
    assert "hello%" in str(sql_arg)
    assert connection_arg == "connection"
    assert read_sql.call_args.kwargs["params"] == {"symbol": "NVDA"}


def test_symbol_list_clause_dedupes_and_binds_values():
    clause, params = conn.symbol_list_clause(["nvda", "AMD", "NVDA"])
    assert clause == ":sym_0, :sym_1"
    assert params == {"sym_0": "AMD", "sym_1": "NVDA"}


def test_backfill_existing_pairs_normalizes_questdb_dates():
    from datetime import date

    from quantamental.scripts.backfill import _existing_pairs

    class FakeWriter:
        def query(self, *_args, **_kwargs):
            return pd.DataFrame(
                [
                    {"symbol": "PSTG", "d": "2026-04-16 20:00:00"},
                    {"symbol": "ASGN", "d": pd.Timestamp("2026-04-23 04:00:00")},
                ]
            )

    pairs = _existing_pairs(date(2026, 4, 1), date(2026, 5, 1), FakeWriter())
    assert pairs == {("PSTG", "2026-04-16"), ("ASGN", "2026-04-23")}


def test_apply_migration_skips_already_applied():
    migration = schema.Migration("001_test", "test", "ALTER TABLE x ADD COLUMN y INT")
    with patch.object(schema, "record_migration") as record:
        applied = schema.apply_migration(migration, applied={"001_test"})

    assert applied is False
    record.assert_not_called()


def test_apply_migration_records_duplicate_tolerated():
    migration = schema.Migration(
        "001_test",
        "test duplicate",
        "ALTER TABLE x ADD COLUMN y INT",
        tolerate_duplicate=True,
    )

    class DuplicateCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql):
            raise RuntimeError("column already exists")

    class DuplicateConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return DuplicateCursor()

        def commit(self):
            raise AssertionError("commit should not be called after duplicate alter")

    with patch.object(schema, "get_connection", return_value=DuplicateConnection()), \
         patch.object(schema, "record_migration") as record:
        applied = schema.apply_migration(migration, applied=set())

    assert applied is True
    record.assert_called_once_with("001_test", "test duplicate")


def test_package_entrypoints_are_declared_and_importable():
    data = tomllib.loads(Path("pyproject.toml").read_text())
    scripts = data["project"]["scripts"]

    expected = {
        "quantamental-pipeline": "quantamental.scripts.daily_pipeline:main",
        "quantamental-check-data": "quantamental.scripts.check_data:main",
        "quantamental-build-universe": "quantamental.scripts.build_universe:main",
        "quantamental-manage-candidates": "quantamental.scripts.manage_candidates:main",
        "quantamental-run-alpha": "quantamental.scripts.run_alpha:main",
        "quantamental-backtest-alpha": "quantamental.scripts.backtest_alpha:main",
        "quantamental-alpha-performance": "quantamental.scripts.alpha_performance:main",
        "quantamental-diagnose-alpha": "quantamental.scripts.diagnose_alpha:main",
        "quantamental-log-earnings-event": "quantamental.scripts.log_earnings_event:main",
        "quantamental-import-earnings-events": "quantamental.scripts.import_earnings_events:main",
        "quantamental-import-earnings-events-csv": "quantamental.scripts.import_earnings_events_csv:main",
    }
    assert scripts == expected

    for target in scripts.values():
        module_name, attr = target.split(":")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attr))


@pytest.mark.parametrize(
    "module_name",
    [
        "quantamental.scripts.daily_pipeline",
        "quantamental.scripts.check_data",
        "quantamental.scripts.build_universe",
        "quantamental.scripts.manage_candidates",
        "quantamental.dashboard.app",
    ],
)
def test_hardened_entry_modules_import(module_name):
    assert importlib.import_module(module_name)


def test_data_quality_events_are_persisted(tmp_path):
    db_path = tmp_path / "meta.db"
    event = DataQualityEvent(
        run_id="run-1",
        asof_date="2026-05-08",
        component="OHLCV",
        symbol="NVDA",
        severity="ERROR",
        check_name="stale_candidate",
        status="FAIL",
        observed="2026-05-01",
        expected="2026-05-08",
        detail="stale_candidate:NVDA",
        fix_hint="backfill",
    )

    inserted = record_data_quality_events([event], path=db_path)
    events = load_data_quality_events(path=db_path)

    assert inserted == 1
    assert len(events) == 1
    row = events.iloc[0]
    assert row["run_id"] == "run-1"
    assert row["symbol"] == "NVDA"
    assert row["status"] == "FAIL"


def test_check_data_audit_issues_writes_structured_events(tmp_path):
    from datetime import date

    from quantamental.scripts.check_data import audit_issues

    db_path = tmp_path / "meta.db"
    inserted = audit_issues(
        ["stale_candidate:AMD", "empty_journal"],
        run_id="health-1",
        asof_date=date(2026, 5, 8),
        path=str(db_path),
    )
    events = load_data_quality_events(path=db_path)

    assert inserted == 2
    assert set(events["component"]) == {"OHLCV", "Portfolio"}
    assert set(events["check_name"]) == {"stale_candidate", "empty_journal"}


def test_check_data_audit_records_success_snapshot(tmp_path):
    from datetime import date

    from quantamental.scripts.check_data import audit_issues

    db_path = tmp_path / "meta.db"
    inserted = audit_issues([], run_id="health-ok", asof_date=date(2026, 5, 8), path=str(db_path))
    events = load_data_quality_events(path=db_path)

    assert inserted == 1
    assert events.iloc[0]["status"] == "OK"
    assert events.iloc[0]["check_name"] == "all_checks_passed"


@dataclass(frozen=True)
class _InputsForManifest:
    ohlcv: pd.DataFrame
    stock_signals: pd.DataFrame
    regime_signals: pd.DataFrame
    sector_signals: pd.DataFrame
    earnings_events: pd.DataFrame


def test_backtest_report_writes_validation_manifest(tmp_path):
    from quantamental.alpha.reporting import build_validation_manifest, save_backtest_report

    class Result:
        metrics = pd.DataFrame([{"strategy": "alpha_strategy", "cagr": 0.1}])
        daily_returns = pd.DataFrame([{"ts": "2026-05-08", "alpha_strategy": 0.01}])
        rebalance_log = pd.DataFrame([{"symbol": "NVDA", "alpha_score": 70}])

    inputs = _InputsForManifest(
        ohlcv=pd.DataFrame(
            [
                {"symbol": "NVDA", "ts": "2026-05-07", "close": 100},
                {"symbol": "SMH", "ts": "2026-05-08", "close": 200},
            ]
        ),
        stock_signals=pd.DataFrame([{"symbol": "NVDA", "ts": "2026-05-08"}]),
        regime_signals=pd.DataFrame([{"ts": "2026-05-08", "regime": "RISK_ON"}]),
        sector_signals=pd.DataFrame([{"ts": "2026-05-08", "composite_score": 1}]),
        earnings_events=pd.DataFrame([{"symbol": "NVDA", "report_date": "2026-05-01"}]),
    )
    manifest = build_validation_manifest(
        report_type="alpha_backtest",
        parameters={"start": "2026-01-01", "end": "2026-05-08", "transaction_cost_bps": 15.0},
        symbols=["NVDA", "SMH", "NVDA"],
        inputs=inputs,
    )

    paths = save_backtest_report(Result(), output_dir=tmp_path, manifest=manifest)
    saved = json.loads(paths["manifest"].read_text())

    assert paths["manifest"].exists()
    assert saved["report_type"] == "alpha_backtest"
    assert saved["universe"] == ["NVDA", "SMH"]
    assert saved["parameters"]["transaction_cost_bps"] == 15.0
    assert saved["inputs"]["ohlcv"]["rows"] == 2
    assert saved["inputs"]["ohlcv"]["max_ts"].startswith("2026-05-08")


def test_alpha_performance_report_writes_and_loads_manifest(tmp_path):
    from quantamental.alpha.reporting import (
        load_latest_alpha_performance,
        save_alpha_performance_report,
        validation_status_from_headline,
    )

    class Report:
        rank_log = pd.DataFrame([{"symbol": "NVDA"}])
        bucket_summary = pd.DataFrame([{"horizon": 20, "bucket": "TOP_BUY"}])
        headline = pd.DataFrame(
            [
                {
                    "horizon": 20,
                    "observations": 50,
                    "top_minus_avoid": 0.03,
                    "mean_rank_ic": 0.08,
                    "rank_dates": 8,
                }
            ]
        )

    status = validation_status_from_headline(Report.headline)
    manifest = {"report_type": "alpha_performance", "validation_status": status}
    paths = save_alpha_performance_report(Report(), output_dir=tmp_path, manifest=manifest)
    loaded = load_latest_alpha_performance(output_dir=tmp_path)

    assert status["status"] == "PASS"
    assert paths["latest_manifest"].exists()
    assert loaded["manifest"]["validation_status"]["status"] == "PASS"
    assert not loaded["headline"].empty


def test_validation_status_fails_when_data_quality_blocked():
    from quantamental.alpha.reporting import validation_status_from_headline

    headline = pd.DataFrame(
        [{"horizon": 20, "observations": 100, "top_minus_avoid": 0.05, "mean_rank_ic": 0.1, "rank_dates": 10}]
    )
    status = validation_status_from_headline(headline, data_quality_status="BLOCKED")

    assert status["status"] == "FAIL"
    assert "data quality" in status["reason"]


def test_pipeline_records_step_audit_events(monkeypatch):
    from quantamental.scripts import daily_pipeline

    captured = {}
    monkeypatch.setattr(daily_pipeline, "STEPS", {"fake_step": lambda: True})
    monkeypatch.setattr("quantamental.data.ingest.questdb_writer.init_schema", lambda: None)
    monkeypatch.setattr("quantamental.portfolio.tracker.init_db", lambda: None)

    def fake_record(events):
        captured["events"] = list(events)
        return len(captured["events"])

    monkeypatch.setattr("quantamental.data.quality.record_data_quality_events", fake_record)

    assert daily_pipeline.run_pipeline("fake_step") is True
    assert len(captured["events"]) == 1
    event = captured["events"][0]
    assert event.component == "Pipeline"
    assert event.check_name == "pipeline_fake_step"
    assert event.status == "OK"
