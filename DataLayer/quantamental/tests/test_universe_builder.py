"""
Unit tests for the research universe builder.
No network — fixtures simulate Wikipedia + Polygon results.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import pytest

from research.universe_builder import (
    apply_static_filters,
    EXCLUDE_SECTORS,
    EXCLUDE_POLYGON_TYPES,
    HARDCODED_TICKER_EXCLUSIONS,
    SPAC_RE,
)


@pytest.fixture
def sp1500_sample():
    """Mini S&P 1500 with one of each pathological case."""
    return pd.DataFrame([
        {"symbol": "NVDA",  "name": "NVIDIA Corp",                 "sector": "Information Technology", "index_source": "sp500"},
        {"symbol": "AAPL",  "name": "Apple Inc.",                  "sector": "Information Technology", "index_source": "sp500"},
        # REIT — should be dropped (sector filter)
        {"symbol": "PLD",   "name": "Prologis Inc.",               "sector": "Real Estate",            "index_source": "sp500"},
        # SPAC — should be dropped (name pattern)
        {"symbol": "ACQX",  "name": "Foo Acquisition Corp",        "sector": "Financials",             "index_source": "sp600"},
        # Trust — should be dropped (name pattern)
        {"symbol": "TRSTX", "name": "Some Trust",                  "sector": "Financials",             "index_source": "sp600"},
        # Preferred — should be dropped (name pattern)
        {"symbol": "PFDX",  "name": "BigCo Preferred Series A",    "sector": "Financials",             "index_source": "sp500"},
    ])


@pytest.fixture
def polygon_sample():
    """What Polygon would return for the sample tickers."""
    return pd.DataFrame([
        {"symbol": "NVDA",  "polygon_type": "CS",   "polygon_active": True},
        {"symbol": "AAPL",  "polygon_type": "CS",   "polygon_active": True},
        {"symbol": "PLD",   "polygon_type": "CS",   "polygon_active": True},   # REIT but Polygon flags as CS
        {"symbol": "ACQX",  "polygon_type": "CS",   "polygon_active": True},
        {"symbol": "TRSTX", "polygon_type": "CS",   "polygon_active": True},
        {"symbol": "PFDX",  "polygon_type": "CS",   "polygon_active": True},
        # An ADR — should be in the input but not survive
        {"symbol": "BABA",  "polygon_type": "ADRC", "polygon_active": True},
    ])


class TestApplyStaticFilters:
    """Static filters operate primarily on Wikipedia data (sector + name).

    Polygon enrichment is optional — these tests verify the Wikipedia-only path
    plus an opt-in test for polygon-aware filtering.
    """

    def test_drops_real_estate_sector(self, sp1500_sample):
        kept, stats = apply_static_filters(sp1500_sample)
        assert "PLD" not in kept["symbol"].tolist()
        assert stats["drops"]["sector_real_estate"] == 1

    def test_drops_spac_acquisition_name(self, sp1500_sample):
        kept, _ = apply_static_filters(sp1500_sample)
        assert "ACQX" not in kept["symbol"].tolist()

    def test_drops_trust_name(self, sp1500_sample):
        kept, _ = apply_static_filters(sp1500_sample)
        assert "TRSTX" not in kept["symbol"].tolist()

    def test_drops_preferred_name(self, sp1500_sample):
        kept, _ = apply_static_filters(sp1500_sample)
        assert "PFDX" not in kept["symbol"].tolist()

    def test_keeps_clean_common_stocks(self, sp1500_sample):
        kept, _ = apply_static_filters(sp1500_sample)
        symbols = kept["symbol"].tolist()
        assert "NVDA" in symbols
        assert "AAPL" in symbols

    def test_no_polygon_data_keeps_unknowns(self, sp1500_sample):
        """Without polygon_df, no ticker is dropped for missing metadata."""
        kept, stats = apply_static_filters(sp1500_sample)
        # NVDA and AAPL should survive even with no polygon data
        assert "NVDA" in kept["symbol"].tolist()
        assert "AAPL" in kept["symbol"].tolist()
        # No "polygon_unknown" drop key should exist
        assert "polygon_unknown" not in stats["drops"]

    def test_polygon_etf_dropped_when_provided(self):
        """When polygon_df IS provided, confirmed ETFs/ADRPs ARE dropped."""
        sp1500 = pd.DataFrame([
            {"symbol": "FOO", "name": "Foo Corp", "sector": "Tech",       "index_source": "sp500"},
            {"symbol": "BAR", "name": "Bar ETF",  "sector": "Financials", "index_source": "sp500"},
        ])
        polygon = pd.DataFrame([
            {"symbol": "FOO", "polygon_type": "CS",  "polygon_active": True},
            {"symbol": "BAR", "polygon_type": "ETF", "polygon_active": True},
        ])
        kept, stats = apply_static_filters(sp1500, polygon)
        assert "FOO" in kept["symbol"].tolist()
        assert "BAR" not in kept["symbol"].tolist()
        assert stats["drops"]["polygon_confirmed_excluded"] == 1

    def test_keeps_adrc_when_polygon_provided(self):
        """ADRC is NOT in EXCLUDE_POLYGON_TYPES anymore — major foreign ADRs are kept."""
        sp1500 = pd.DataFrame([
            {"symbol": "TSM", "name": "Taiwan Semi", "sector": "Tech", "index_source": "sp500"},
        ])
        polygon = pd.DataFrame([
            {"symbol": "TSM", "polygon_type": "ADRC", "polygon_active": True},
        ])
        kept, _ = apply_static_filters(sp1500, polygon)
        # TSM is in BASE_CANDIDATES — must survive
        assert "TSM" in kept["symbol"].tolist()

    def test_stats_shape(self, sp1500_sample):
        _, stats = apply_static_filters(sp1500_sample)
        assert "initial_count" in stats
        assert "final_count" in stats
        assert "drops" in stats
        assert stats["initial_count"] >= stats["final_count"]

    def test_hardcoded_exclusion_drops_baba(self):
        """HARDCODED_TICKER_EXCLUSIONS removes Chinese ADRs even if Wikipedia listed."""
        sp1500 = pd.DataFrame([
            {"symbol": "NVDA", "name": "NVIDIA",  "sector": "Tech",                "index_source": "sp500"},
            {"symbol": "BABA", "name": "Alibaba", "sector": "Consumer Discretionary","index_source": "sp500"},
            {"symbol": "JD",   "name": "JD.com",  "sector": "Consumer Discretionary","index_source": "sp500"},
        ])
        kept, stats = apply_static_filters(sp1500)
        symbols = kept["symbol"].tolist()
        assert "NVDA" in symbols
        assert "BABA" not in symbols
        assert "JD" not in symbols
        assert stats["drops"]["hardcoded_exclusions"] == 2

    def test_hardcoded_exclusion_keeps_clean_tickers(self):
        """Tickers not in the hardcoded list pass through unchanged."""
        sp1500 = pd.DataFrame([
            {"symbol": "AAPL", "name": "Apple",       "sector": "Tech", "index_source": "sp500"},
            {"symbol": "MSFT", "name": "Microsoft",   "sector": "Tech", "index_source": "sp500"},
        ])
        kept, stats = apply_static_filters(sp1500)
        assert sorted(kept["symbol"].tolist()) == ["AAPL", "MSFT"]
        assert stats["drops"]["hardcoded_exclusions"] == 0


class TestSpacRegex:
    @pytest.mark.parametrize("name,should_match", [
        ("Foo Acquisition Corp",        True),
        ("Bar Trust",                   True),
        ("Baz SPAC",                    True),
        ("BigCo Preferred Series A",    True),
        ("Some Co Warrant",             True),
        ("Foo Bar Unit",                True),
        ("Apple Inc.",                  False),
        ("NVIDIA Corp",                 False),
        ("Microsoft Corporation",       False),
        # "Trustmark" doesn't match \bTRUST\b — no word boundary after "Trust".
        # This is desired: prevents false positives on legitimate companies.
        ("Trustmark Inc.",              False),
    ])
    def test_pattern_matching(self, name, should_match):
        assert bool(SPAC_RE.search(name)) == should_match


class TestExcludeConstants:
    def test_real_estate_in_excluded_sectors(self):
        assert "Real Estate" in EXCLUDE_SECTORS

    def test_etf_types_excluded(self):
        assert "ETF" in EXCLUDE_POLYGON_TYPES
        assert "ETN" in EXCLUDE_POLYGON_TYPES

    def test_adrc_NOT_excluded(self):
        # Major foreign ADRs (TSM, ASML, NVO) are legitimate trading instruments.
        # Only ADRP/ADRR/ADRW (preferred/right/warrant variants) are excluded.
        assert "ADRC" not in EXCLUDE_POLYGON_TYPES
