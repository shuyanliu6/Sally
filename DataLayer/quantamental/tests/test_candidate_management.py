"""
Tests for the candidate list mechanism (load + manage_candidates CLI helpers).
No DB, no network — all use temp paths.
"""

import json, importlib
from pathlib import Path

import pytest


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Replace the config file paths with temp paths so tests don't pollute real files."""
    candidate_file = tmp_path / "candidate_list.json"
    research_file  = tmp_path / "research_tickers.json"

    import quantamental.config.universe as universe_mod

    monkeypatch.setattr(universe_mod, "_CANDIDATE_FILE", candidate_file)
    monkeypatch.setattr(universe_mod, "_RESEARCH_FILE",  research_file)

    return {"candidate": candidate_file, "research": research_file, "module": universe_mod}


class TestLoadCandidateList:
    def test_falls_back_to_base_when_no_json(self, isolated_config):
        u = isolated_config["module"]
        result = u.load_candidate_list()
        assert sorted(result) == sorted(u.BASE_CANDIDATE_TICKERS)

    def test_uses_json_when_present(self, isolated_config):
        u = isolated_config["module"]
        isolated_config["candidate"].write_text(json.dumps({"tickers": ["NVDA", "AMD"]}))
        result = u.load_candidate_list()
        assert sorted(result) == ["AMD", "NVDA"]

    def test_uppercases_tickers(self, isolated_config):
        u = isolated_config["module"]
        isolated_config["candidate"].write_text(json.dumps({"tickers": ["nvda", "Amd"]}))
        result = u.load_candidate_list()
        assert sorted(result) == ["AMD", "NVDA"]

    def test_falls_back_on_corrupt_json(self, isolated_config):
        u = isolated_config["module"]
        isolated_config["candidate"].write_text("not valid json {{{")
        result = u.load_candidate_list()
        assert sorted(result) == sorted(u.BASE_CANDIDATE_TICKERS)

    def test_falls_back_on_empty_tickers(self, isolated_config):
        u = isolated_config["module"]
        isolated_config["candidate"].write_text(json.dumps({"tickers": []}))
        result = u.load_candidate_list()
        # Empty list → fallback to BASE
        assert sorted(result) == sorted(u.BASE_CANDIDATE_TICKERS)


class TestLoadResearchUniverse:
    def test_falls_back_to_candidate_list_when_no_research_file(self, isolated_config):
        u = isolated_config["module"]
        result = u.load_research_universe()
        assert sorted(result) == sorted(u.BASE_CANDIDATE_TICKERS)

    def test_uses_research_file_when_present(self, isolated_config):
        u = isolated_config["module"]
        isolated_config["research"].write_text(
            json.dumps({"tickers": ["AAPL", "GOOGL", "META", "MSFT", "NVDA"]})
        )
        result = u.load_research_universe()
        assert sorted(result) == ["AAPL", "GOOGL", "META", "MSFT", "NVDA"]

    def test_research_takes_priority_over_candidate(self, isolated_config):
        u = isolated_config["module"]
        # Both files exist — research_tickers should win
        isolated_config["candidate"].write_text(json.dumps({"tickers": ["NVDA"]}))
        isolated_config["research"].write_text(json.dumps({"tickers": ["AAPL", "MSFT"]}))
        result = u.load_research_universe()
        assert sorted(result) == ["AAPL", "MSFT"]


class TestSourceReporting:
    def test_candidate_source_default(self, isolated_config):
        u = isolated_config["module"]
        assert "BASE" in u.candidate_list_source()

    def test_candidate_source_with_json(self, isolated_config):
        u = isolated_config["module"]
        isolated_config["candidate"].write_text(json.dumps({"tickers": ["NVDA"]}))
        assert "candidate_list.json" in u.candidate_list_source()

    def test_research_source_default(self, isolated_config):
        u = isolated_config["module"]
        assert "fallback" in u.research_universe_source()

    def test_research_source_with_json(self, isolated_config):
        u = isolated_config["module"]
        isolated_config["research"].write_text(json.dumps({"tickers": ["NVDA"]}))
        assert "research_tickers.json" in u.research_universe_source()


class TestBackwardsCompatibility:
    def test_all_tickers_alias_still_works(self, isolated_config):
        u = isolated_config["module"]
        # Old import path
        assert u.ALL_TICKERS == u.BASE_CANDIDATE_TICKERS

    def test_universe_dict_alias_still_works(self, isolated_config):
        u = isolated_config["module"]
        assert u.UNIVERSE == u.BASE_CANDIDATES

    def test_benchmarks_alias_still_works(self, isolated_config):
        u = isolated_config["module"]
        assert "SPY" in u.BENCHMARKS
        assert "QQQ" in u.BENCHMARKS

    def test_splits_equities_and_etfs(self, isolated_config):
        u = isolated_config["module"]
        isolated_config["candidate"].write_text(
            json.dumps(
                {
                    "sectors": {
                        "upstream_compute": ["NVDA", "AMD"],
                        "benchmarks": ["SPY", "QQQ", "SMH"],
                        "non_us": ["EWY", "ASML"],
                    }
                }
            )
        )

        equities = u.load_equity_candidate_list()
        etfs = u.load_etf_candidate_list()

        assert equities == ["AMD", "ASML", "NVDA"]
        assert etfs == ["EWY", "QQQ", "SMH", "SPY"]
        assert not set(equities) & set(etfs)

    def test_instrument_classifier_labels_etfs(self, isolated_config):
        u = isolated_config["module"]

        assert u.is_etf_symbol("SMH")
        assert u.is_etf_symbol("EWY", "non_us")
        assert u.is_etf_symbol("NVDA", "benchmarks")
        assert not u.is_etf_symbol("NVDA", "upstream_compute")
