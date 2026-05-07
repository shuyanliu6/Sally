"""
Unit tests for macro signal scorers.
No network calls, no database — all synthetic DataFrames.
"""

import numpy as np
import pandas as pd
import pytest

from quantamental.signals.macro import (
    score_credit_spread,
    score_fed_balance,
    score_vix,
    score_yield,
    compute_all_signals,
)
from quantamental.signals.aggregator import (
    adjusted_rsi_score,
    classify_regime,
    compute_confirmed_regime,
    macro_override_blocks_buys,
    map_action,
    normalize_composite,
    run_composite,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_yield_df(values: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2022-01-01", periods=len(values), freq="B")
    return pd.DataFrame({"ts": dates, "value": values})


def make_vix_df(value: float) -> pd.DataFrame:
    return pd.DataFrame({"ts": [pd.Timestamp("2024-01-01")], "value": [value]})


def make_fed_df(values: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2020-01-01", periods=len(values), freq="W")
    return pd.DataFrame({"ts": dates, "value": values})


def make_credit_df(values: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2022-01-01", periods=len(values), freq="B")
    return pd.DataFrame({"ts": dates, "value": values})


# ── score_vix ─────────────────────────────────────────────────────────────────

class TestScoreVix:
    def test_extreme_low(self):
        assert score_vix(12.0) == 2

    def test_boundary_at_15(self):
        # exactly 15 → not < 15, falls to next bucket (15-20 = +1)
        assert score_vix(15.0) == 1

    def test_low_volatility(self):
        assert score_vix(17.5) == 1

    def test_neutral(self):
        assert score_vix(22.0) == 0

    def test_high_volatility(self):
        assert score_vix(30.0) == -1

    def test_panic(self):
        assert score_vix(40.0) == -2

    def test_boundary_at_35(self):
        # exactly 35 → not < 35 → panic → -2
        assert score_vix(35.0) == -2


# ── score_yield ───────────────────────────────────────────────────────────────

class TestScoreYield:
    def _trending_down(self, final_level=3.5):
        """Build a 65-day series where yield steadily falls (bullish)."""
        # Start at 5.5, end at final_level — 20MA will be below 60MA
        vals = np.linspace(5.5, final_level, 65).tolist()
        return make_yield_df(vals)

    def _trending_up(self, final_level=5.5):
        """Build a 65-day series where yield steadily rises (bearish)."""
        vals = np.linspace(3.0, final_level, 65).tolist()
        return make_yield_df(vals)

    def test_insufficient_data_returns_zero(self):
        df = make_yield_df([4.0] * 30)
        assert score_yield(df) == 0

    def test_strong_bullish(self):
        # Yields falling AND final level < 4.0
        df = self._trending_down(final_level=3.5)
        assert score_yield(df) == 2

    def test_moderate_bullish(self):
        # Yields falling but final level >= 4.0
        df = self._trending_down(final_level=4.3)
        assert score_yield(df) == 1

    def test_strong_bearish(self):
        # Yields rising AND final level > 5.0
        df = self._trending_up(final_level=5.5)
        assert score_yield(df) == -2

    def test_moderate_bearish(self):
        # Yields rising but final level <= 5.0
        df = self._trending_up(final_level=4.8)
        assert score_yield(df) == -1

    def test_neutral_flat(self):
        # Flat series — 20MA and 60MA nearly identical
        df = make_yield_df([4.0] * 65)
        assert score_yield(df) == 0


# ── score_fed_balance ─────────────────────────────────────────────────────────

class TestScoreFedBalance:
    def test_insufficient_data_returns_zero(self):
        df = make_fed_df([8e12] * 5)
        assert score_fed_balance(df) == 0

    def test_strong_expansion(self):
        # Balance sheet growing rapidly week-over-week
        base = 8e12
        vals = [base + i * 100e9 for i in range(20)]  # +100B/week → large pct
        df = make_fed_df(vals)
        assert score_fed_balance(df) == 2

    def test_mild_expansion(self):
        # Small but positive growth
        base = 8e12
        vals = [base + i * 1e9 for i in range(20)]   # +1B/week → tiny pct
        df = make_fed_df(vals)
        assert score_fed_balance(df) == 1

    def test_contraction(self):
        base = 8e12
        vals = [base - i * 50e9 for i in range(20)]  # shrinking
        df = make_fed_df(vals)
        assert score_fed_balance(df) in (-2, -1)


# ── score_credit_spread ───────────────────────────────────────────────────────

class TestScoreCreditSpread:
    def test_insufficient_data_returns_zero(self):
        df = make_credit_df([100.0] * 30)
        assert score_credit_spread(df) == 0

    def test_tightening_spreads_bullish(self):
        # Spreads declining — 20MA below 60MA
        vals = np.linspace(200, 80, 65).tolist()
        df = make_credit_df(vals)
        assert score_credit_spread(df) == 1

    def test_widening_spreads_bearish(self):
        # Spreads rising but below 200bps
        vals = np.linspace(80, 150, 65).tolist()
        df = make_credit_df(vals)
        assert score_credit_spread(df) == -1

    def test_widening_above_200bps_strong_bearish(self):
        # Spreads widening AND above 200bps
        vals = np.linspace(150, 250, 65).tolist()
        df = make_credit_df(vals)
        assert score_credit_spread(df) == -2


# ── classify_regime ───────────────────────────────────────────────────────────

class TestComputeConfirmedRegime:
    """D5 — 2-day regime confirmation rule."""

    def test_first_observation_uses_today(self):
        """No history → today's regime becomes confirmed immediately."""
        result = compute_confirmed_regime("RISK_ON", yesterday_regime=None, yesterday_confirmed=None)
        assert result == "RISK_ON"

    def test_two_consecutive_same_days_confirm(self):
        """Today == yesterday raw → today's regime is confirmed."""
        result = compute_confirmed_regime(
            today_regime="RISK_OFF",
            yesterday_regime="RISK_OFF",
            yesterday_confirmed="NEUTRAL",  # was previously a different confirmed
        )
        assert result == "RISK_OFF"

    def test_one_day_flip_does_not_confirm(self):
        """Today != yesterday raw → carry forward yesterday's confirmed."""
        result = compute_confirmed_regime(
            today_regime="RISK_OFF",     # noisy flip
            yesterday_regime="RISK_ON",
            yesterday_confirmed="RISK_ON",
        )
        assert result == "RISK_ON"  # held steady

    def test_flip_back_does_not_confirm_either(self):
        """RISK_ON → RISK_OFF (1 day) → RISK_ON (today). Confirmed stays RISK_ON."""
        # Day 2: RISK_ON yesterday, RISK_OFF today → confirmed stays RISK_ON
        # Day 3: RISK_OFF yesterday raw, RISK_ON today → still doesn't match
        result = compute_confirmed_regime(
            today_regime="RISK_ON",
            yesterday_regime="RISK_OFF",     # the one-day blip
            yesterday_confirmed="RISK_ON",
        )
        assert result == "RISK_ON"  # unchanged

    def test_two_consecutive_flip_does_confirm(self):
        """The flip persisted 2 days → now confirm."""
        result = compute_confirmed_regime(
            today_regime="RISK_OFF",
            yesterday_regime="RISK_OFF",  # 2nd consecutive day of RISK_OFF
            yesterday_confirmed="RISK_ON",
        )
        assert result == "RISK_OFF"

    def test_no_yesterday_confirmed_falls_back_to_today(self):
        """Edge case: yesterday raw exists but confirmed missing → today wins."""
        result = compute_confirmed_regime(
            today_regime="NEUTRAL",
            yesterday_regime="RISK_ON",
            yesterday_confirmed=None,
        )
        assert result == "NEUTRAL"


class TestClassifyRegime:
    @pytest.mark.parametrize("score,expected", [
        (8,  "RISK_ON"),
        (5,  "RISK_ON"),
        (4,  "MODERATE_ON"),
        (2,  "MODERATE_ON"),
        (1,  "NEUTRAL"),
        (-1, "NEUTRAL"),
        (-2, "MODERATE_OFF"),
        (-4, "MODERATE_OFF"),
        (-5, "RISK_OFF"),
        (-8, "RISK_OFF"),
    ])
    def test_thresholds(self, score, expected):
        assert classify_regime(score) == expected


# ── compute_all_signals integration ──────────────────────────────────────────

class TestComputeAllSignals:
    def test_returns_all_keys(self):
        yield_df = make_yield_df(np.linspace(5.5, 3.5, 65).tolist())
        vix_df = make_vix_df(12.0)
        fed_df = make_fed_df([8e12 + i * 100e9 for i in range(20)])
        credit_df = make_credit_df(np.linspace(200, 80, 65).tolist())

        result = compute_all_signals(yield_df, vix_df, fed_df, credit_df)
        for key in ("yield_10y_signal", "vix_signal", "fed_bs_signal",
                    "credit_spread_signal", "composite_score"):
            assert key in result

    def test_composite_is_sum_of_signals(self):
        yield_df = make_yield_df(np.linspace(5.5, 3.5, 65).tolist())
        vix_df = make_vix_df(12.0)
        fed_df = make_fed_df([8e12 + i * 100e9 for i in range(20)])
        credit_df = make_credit_df(np.linspace(200, 80, 65).tolist())

        r = compute_all_signals(yield_df, vix_df, fed_df, credit_df)
        expected = (r["yield_10y_signal"] + r["vix_signal"] +
                    r["fed_bs_signal"] + r["credit_spread_signal"])
        assert r["composite_score"] == expected

    def test_composite_within_range(self):
        yield_df = make_yield_df(np.linspace(5.5, 3.5, 65).tolist())
        vix_df = make_vix_df(12.0)
        fed_df = make_fed_df([8e12 + i * 100e9 for i in range(20)])
        credit_df = make_credit_df(np.linspace(200, 80, 65).tolist())

        r = compute_all_signals(yield_df, vix_df, fed_df, credit_df)
        assert -8 <= r["composite_score"] <= 8


# ── Month 2: 3-layer composite aggregator ─────────────────────────────────────

class TestNormalizeComposite:
    def test_neutral_inputs_neutral_output(self):
        assert normalize_composite(0, 0, 0.0) == 0.0

    def test_max_positive(self):
        # +8 macro, +8 sector, +7 stock = max possible (18.6 weighted)
        result = normalize_composite(8, 8, 7.0)
        assert result == 9.0

    def test_max_negative(self):
        result = normalize_composite(-8, -8, -7.0)
        assert result == -9.0

    def test_macro_dominates(self):
        # Macro +8 alone vs sector +8 alone — macro should produce higher
        macro_only = normalize_composite(8, 0, 0.0)
        sector_only = normalize_composite(0, 8, 0.0)
        assert macro_only > sector_only

    def test_stock_lowest_weight(self):
        sector_only = normalize_composite(0, 8, 0.0)
        stock_only = normalize_composite(0, 0, 7.0)
        assert sector_only > stock_only


class TestMapAction:
    @pytest.mark.parametrize("score,expected_regime", [
        (9.0, "STRONG_BUY"),
        (7.0, "STRONG_BUY"),
        (6.9, "BUY"),
        (4.0, "BUY"),
        (3.5, "MILD_BUY"),
        (1.0, "MILD_BUY"),
        (0.5, "NEUTRAL"),
        (-1.0, "NEUTRAL"),
        (-1.5, "MILD_SELL"),
        (-4.0, "MILD_SELL"),
        (-5.0, "SELL"),
        (-7.0, "SELL"),
        (-7.5, "STRONG_SELL"),
        (-9.0, "STRONG_SELL"),
    ])
    def test_thresholds(self, score, expected_regime):
        regime, action = map_action(score)
        assert regime == expected_regime
        assert action  # non-empty string


class TestAdjustedRsiScore:
    def test_strong_uptrend_softens_overbought(self):
        # EMA +2, RSI -1 → 0 (overbought in uptrend = momentum, not exhaustion)
        assert adjusted_rsi_score(rsi_score=-1, ema_score=2) == 0

    def test_strong_downtrend_softens_oversold(self):
        # EMA -2, RSI +1 → 0 (oversold in downtrend = falling knife)
        assert adjusted_rsi_score(rsi_score=1, ema_score=-2) == 0

    def test_passes_through_otherwise(self):
        assert adjusted_rsi_score(rsi_score=2, ema_score=2) == 2
        assert adjusted_rsi_score(rsi_score=-2, ema_score=2) == -2
        assert adjusted_rsi_score(rsi_score=1, ema_score=0) == 1


class TestMacroOverride:
    def test_blocks_when_risk_off(self):
        assert macro_override_blocks_buys(-5) is True
        assert macro_override_blocks_buys(-8) is True

    def test_allows_at_threshold(self):
        assert macro_override_blocks_buys(-4) is False  # threshold inclusive on safe side
        assert macro_override_blocks_buys(0) is False
        assert macro_override_blocks_buys(8) is False


class TestRunComposite:
    def test_neutral_inputs(self):
        result = run_composite(0, 0, 0.0, persist=False)
        assert result["regime"] == "NEUTRAL"
        assert result["normalized_score"] == 0.0

    def test_strong_bullish(self):
        result = run_composite(8, 8, 7.0, persist=False)
        assert result["regime"] == "STRONG_BUY"
        assert result["normalized_score"] == 9.0

    def test_macro_override_overrides_buy(self):
        # Macro -5 (RISK_OFF) but sector +8, stock +7 → would normally be BUY
        # Override forces NEUTRAL
        result = run_composite(-5, 8, 7.0, persist=False)
        assert result["regime"] == "NEUTRAL"
        assert "override" in result["action"].lower()

    def test_returns_all_keys(self):
        result = run_composite(2, 3, 1.5, persist=False)
        for key in ("ts", "macro_score", "sector_score", "avg_stock_score",
                    "weighted_composite", "normalized_score", "regime", "action"):
            assert key in result
