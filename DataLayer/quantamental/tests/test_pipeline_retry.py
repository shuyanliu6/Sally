"""
Unit tests for pipeline retry and resume logic.
No network, no DB — mocks all step functions.
"""

import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path


# ── Helpers ───────────────────────────────────────────────────────────────────

def _import_pipeline():
    import importlib
    import scripts.daily_pipeline as p
    importlib.reload(p)   # fresh state each test
    return p


# ── with_retry tests ──────────────────────────────────────────────────────────

class TestWithRetry:
    def test_succeeds_on_first_attempt(self):
        from scripts.daily_pipeline import with_retry
        calls = []
        def fn():
            calls.append(1)
            return True
        result = with_retry(fn, "test_step", max_retries=3, delay=0)
        assert result is True
        assert len(calls) == 1

    def test_retries_then_succeeds(self):
        from scripts.daily_pipeline import with_retry
        calls = []
        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("flaky")
            return True
        result = with_retry(fn, "test_step", max_retries=3, delay=0)
        assert result is True
        assert len(calls) == 3

    def test_raises_after_max_retries(self):
        from scripts.daily_pipeline import with_retry
        calls = []
        def fn():
            calls.append(1)
            raise ConnectionError("always fails")
        with pytest.raises(ConnectionError):
            with_retry(fn, "test_step", max_retries=3, delay=0)
        assert len(calls) == 3

    def test_exponential_delay(self):
        from scripts.daily_pipeline import with_retry
        slept = []
        def fn():
            raise ValueError("boom")
        with patch("scripts.daily_pipeline.time.sleep", side_effect=lambda s: slept.append(s)):
            with pytest.raises(ValueError):
                with_retry(fn, "test_step", max_retries=3, delay=5)
        # attempt 1 → sleep 5, attempt 2 → sleep 10, attempt 3 → raise (no sleep)
        assert slept == [5, 10]


# ── State file tests ──────────────────────────────────────────────────────────

class TestStateFile:
    def test_load_state_missing_returns_empty(self, tmp_path):
        p = _import_pipeline()
        with patch("scripts.daily_pipeline._state_path", return_value=tmp_path / "state.json"):
            state = p._load_state()
        assert state == {"completed": [], "failed": []}

    def test_load_state_force_returns_empty(self, tmp_path):
        p = _import_pipeline()
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"completed": ["fetch_market"], "failed": []}))
        with patch("scripts.daily_pipeline._state_path", return_value=path):
            state = p._load_state(force=True)
        assert state["completed"] == []

    def test_save_and_reload_state(self, tmp_path):
        p = _import_pipeline()
        path = tmp_path / "state.json"
        state = {"completed": ["fetch_market", "fetch_macro"], "failed": ["calc_signals"]}
        with patch("scripts.daily_pipeline._state_path", return_value=path):
            p._save_state(state)
            loaded = p._load_state()
        assert loaded["completed"] == ["fetch_market", "fetch_macro"]
        assert loaded["failed"] == ["calc_signals"]


# ── run_pipeline resume tests ─────────────────────────────────────────────────

class TestRunPipeline:
    def _make_mock_steps(self, fail_steps=None):
        """Return a dict of mock step functions, optionally failing some."""
        fail_steps = fail_steps or []
        mocks = {}
        for name in ["fetch_market", "fetch_macro", "calc_signals",
                     "update_portfolio", "check_stops"]:
            if name in fail_steps:
                mocks[name] = MagicMock(side_effect=RuntimeError(f"{name} failed"))
            else:
                mocks[name] = MagicMock(return_value=True)
        return mocks

    # init_schema and init_db are imported inside run_pipeline as local imports,
    # so we patch them at their source modules.
    _PATCHES = [
        "data.ingest.questdb_writer.init_schema",
        "portfolio.tracker.init_db",
    ]

    def _run(self, p, tmp_path, mocks, step="all", force=False):
        path = tmp_path / "state.json"
        patches = [patch(t) for t in self._PATCHES]
        with patch("scripts.daily_pipeline._state_path", return_value=path), \
             patch("scripts.daily_pipeline.STEPS", mocks):
            for pt in patches:
                pt.start()
            try:
                p.run_pipeline(step, force=force, max_retries=1, retry_delay=0)
            finally:
                for pt in patches:
                    pt.stop()
        return path

    def test_all_steps_run_on_fresh_state(self, tmp_path):
        p = _import_pipeline()
        mocks = self._make_mock_steps()
        self._run(p, tmp_path, mocks)
        for m in mocks.values():
            m.assert_called_once()

    def test_completed_steps_are_skipped_on_resume(self, tmp_path):
        p = _import_pipeline()
        mocks = self._make_mock_steps()
        state_path = tmp_path / "state.json"
        # Simulate fetch_market already completed
        state_path.write_text(json.dumps({"completed": ["fetch_market"], "failed": []}))
        self._run(p, tmp_path, mocks)
        # fetch_market should be skipped
        mocks["fetch_market"].assert_not_called()
        # all others should run
        for name in ["fetch_macro", "calc_signals", "update_portfolio", "check_stops"]:
            mocks[name].assert_called_once()

    def test_force_reruns_completed_steps(self, tmp_path):
        p = _import_pipeline()
        mocks = self._make_mock_steps()
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({"completed": ["fetch_market", "fetch_macro"], "failed": []}))
        self._run(p, tmp_path, mocks, force=True)
        # Everything should run
        for m in mocks.values():
            m.assert_called_once()

    def test_failed_step_saved_to_state(self, tmp_path):
        p = _import_pipeline()
        mocks = self._make_mock_steps(fail_steps=["calc_signals"])
        state_path = self._run(p, tmp_path, mocks)
        state = json.loads(state_path.read_text())
        assert "calc_signals" in state["failed"]
        assert "fetch_market" in state["completed"]
        assert "fetch_macro" in state["completed"]

    def test_single_step_ignores_state(self, tmp_path):
        p = _import_pipeline()
        mocks = self._make_mock_steps()
        state_path = tmp_path / "state.json"
        # Mark fetch_market as completed — should still run when targeted directly
        state_path.write_text(json.dumps({"completed": ["fetch_market"], "failed": []}))
        self._run(p, tmp_path, mocks, step="fetch_market")
        mocks["fetch_market"].assert_called_once()
        # Other steps should not run
        mocks["fetch_macro"].assert_not_called()
