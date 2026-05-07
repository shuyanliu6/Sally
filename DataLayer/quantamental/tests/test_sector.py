"""
Unit tests for sector signals (Month 2 §3).
No DB, no network — synthetic series only.
"""

import pandas as pd
import pytest
import numpy as np

from quantamental.signals.sector import (
    calc_sox_spx_signal,
    compute_sector_composite,
    EMA_FAST,
    EMA_SLOW,
)
from quantamental.signals.sector_ai_infra import (
    _score_tsmc,
    _shift_month,
    add_tsmc_revenue,
    add_capex_surprise,
    calc_capex_signal_for_quarter,
    add_api_pricing,
    latest_api_pricing_signal,
    latest_tsmc_signal,
    latest_capex_signal,
    init_ai_infra_db,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_series(values: list[float], start: str = "2024-01-01") -> pd.Series:
    idx = pd.date_range(start, periods=len(values), freq="B")
    return pd.Series(values, index=idx)


# ── Signal A: SOX/SPX ─────────────────────────────────────────────────────────

class TestSoxSpxSignal:
    def test_insufficient_data_returns_neutral(self):
        smh = make_series([100] * 30)
        spy = make_series([100] * 30)
        result = calc_sox_spx_signal(smh, spy)
        assert result["signal"] == 0

    def test_strong_bullish_at_20day_high(self):
        # SMH steadily outpaces SPY — ratio rising, ending at 20-day high
        smh = make_series(np.linspace(100, 130, 80).tolist())
        spy = make_series([100.0] * 80)
        result = calc_sox_spx_signal(smh, spy)
        assert result["signal"] == 2
        assert result["ema_fast"] > result["ema_slow"]

    def test_moderate_bullish(self):
        # Bullish but not at the 20-day extreme — ratio peaked then dipped slightly
        vals = list(np.linspace(100, 130, 60)) + [129, 128, 127, 126, 125] * 4
        smh = make_series(vals)
        spy = make_series([100.0] * len(vals))
        result = calc_sox_spx_signal(smh, spy)
        assert result["signal"] == 1

    def test_strong_bearish_at_20day_low(self):
        smh = make_series(np.linspace(130, 100, 80).tolist())
        spy = make_series([100.0] * 80)
        result = calc_sox_spx_signal(smh, spy)
        assert result["signal"] == -2

    def test_moderate_bearish(self):
        vals = list(np.linspace(130, 100, 60)) + [101, 102, 103, 104, 105] * 4
        smh = make_series(vals)
        spy = make_series([100.0] * len(vals))
        result = calc_sox_spx_signal(smh, spy)
        assert result["signal"] == -1

    def test_neutral_when_in_band(self):
        # SMH and SPY move together — ratio constant → fast == slow
        smh = make_series([100.0] * 80)
        spy = make_series([100.0] * 80)
        result = calc_sox_spx_signal(smh, spy)
        assert result["signal"] == 0

    def test_returns_all_keys(self):
        smh = make_series([100.0] * 80)
        spy = make_series([100.0] * 80)
        result = calc_sox_spx_signal(smh, spy)
        for key in ("ratio", "ema_fast", "ema_slow", "signal"):
            assert key in result


# ── Sector composite ──────────────────────────────────────────────────────────

class TestSectorComposite:
    def test_sums_signals(self):
        assert compute_sector_composite(2, 1, 1, 1) == 5

    def test_clamps_to_max(self):
        assert compute_sector_composite(2, 2, 2, 2) == 8

    def test_clamps_to_min(self):
        assert compute_sector_composite(-2, -2, -2, -2) == -8

    def test_defaults_zero(self):
        assert compute_sector_composite(2) == 2  # B/C/D default to 0


# ── Helpers ───────────────────────────────────────────────────────────────────

class TestShiftMonth:
    def test_back_one_year(self):
        assert _shift_month("2026-01", -12) == "2025-01"

    def test_back_one_month(self):
        assert _shift_month("2026-01", -1) == "2025-12"

    def test_forward_three(self):
        assert _shift_month("2026-01", 3) == "2026-04"


class TestScoreTsmc:
    def test_no_data_returns_zero(self):
        assert _score_tsmc(None, None) == 0

    def test_explosive_growth_accelerating(self):
        # YoY > 30% AND accelerating
        assert _score_tsmc(35.0, prev_ma3=25.0) == 2

    def test_explosive_growth_decelerating(self):
        # YoY > 30% but NOT accelerating — drops to +1 per spec
        assert _score_tsmc(35.0, prev_ma3=40.0) == 1

    def test_healthy_growth(self):
        assert _score_tsmc(20.0, prev_ma3=10.0) == 1

    def test_normalising(self):
        assert _score_tsmc(5.0, prev_ma3=10.0) == 0

    def test_contraction_first_month(self):
        assert _score_tsmc(-5.0, prev_ma3=2.0) == -1

    def test_sustained_contraction(self):
        # Two consecutive negative months
        assert _score_tsmc(-5.0, prev_ma3=-3.0) == -2


# ── Signal B/C/D end-to-end via SQLite ────────────────────────────────────────

class TestSignalBPersistence:
    def test_full_year_yoy_calc(self, tmp_path):
        path = str(tmp_path / "test.db")
        init_ai_infra_db(path)

        # Seed prior year
        add_tsmc_revenue("2025-01", 200.0, path=path)
        # New month — should compute YoY
        result = add_tsmc_revenue("2026-01", 260.0, path=path)
        assert result["yoy_growth"] == pytest.approx(30.0)

    def test_latest_signal_reads_back(self, tmp_path):
        path = str(tmp_path / "test.db")
        # No data → 0
        assert latest_tsmc_signal(path) == 0

        add_tsmc_revenue("2025-01", 200.0, path=path)
        add_tsmc_revenue("2026-01", 260.0, path=path)  # +30% YoY → score
        assert latest_tsmc_signal(path) != 0


class TestSignalCPersistence:
    def test_insufficient_data_returns_zero(self, tmp_path):
        path = str(tmp_path / "test.db")
        init_ai_infra_db(path)
        add_capex_surprise("2026-Q1", "META", 8.0, 7.0, path=path)
        # Only 1 company → neutral
        assert calc_capex_signal_for_quarter("2026-Q1", path=path) == 0

    def test_strong_beat_scores_plus_two(self, tmp_path):
        path = str(tmp_path / "test.db")
        init_ai_infra_db(path)
        # All 4 companies beat by 15%+
        for co in ("META", "MSFT", "GOOGL", "AMZN"):
            add_capex_surprise("2026-Q1", co, 11.5, 10.0, path=path)  # +15%
        assert calc_capex_signal_for_quarter("2026-Q1", path=path) == 2

    def test_strong_miss_scores_minus_two(self, tmp_path):
        path = str(tmp_path / "test.db")
        init_ai_infra_db(path)
        for co in ("META", "MSFT", "GOOGL"):
            add_capex_surprise("2026-Q1", co, 8.5, 10.0, path=path)  # -15%
        assert calc_capex_signal_for_quarter("2026-Q1", path=path) == -2


class TestSignalDPricing:
    def test_no_data_returns_zero(self, tmp_path):
        path = str(tmp_path / "test.db")
        assert latest_api_pricing_signal(path) == 0

    def test_stable_pricing_returns_one(self, tmp_path):
        path = str(tmp_path / "test.db")
        init_ai_infra_db(path)
        # Same price for 100 days
        for i in range(100):
            d = (pd.Timestamp("2026-01-01") + pd.Timedelta(days=i)).strftime("%Y-%m-%d")
            add_api_pricing(d, "OpenAI", "gpt-5", 5.0, 15.0, path=path)
        assert latest_api_pricing_signal(path) == 1

    def test_deepseek_shock(self, tmp_path):
        path = str(tmp_path / "test.db")
        init_ai_infra_db(path)
        # 90 days ago: $5/M
        add_api_pricing("2026-01-26", "OpenAI", "gpt-5", 5.0, 15.0, path=path)
        # Today: $1.5/M (-70%)
        add_api_pricing("2026-04-26", "OpenAI", "gpt-5", 1.5, 4.0, path=path)
        signal = latest_api_pricing_signal(path)
        assert signal == -2  # DeepSeek-scale shock
