"""Hardening tests for packaging, query params, and migrations."""

import importlib
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from quantamental.data.ingest import questdb_connection as conn
from quantamental.data.ingest import questdb_schema as schema


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
