"""
Unit tests for stop-loss checker. No DB, no network.
"""

import pandas as pd
import pytest
from quantamental.portfolio.stoploss import check_stops, format_stop_alerts


def make_positions(rows):
    return pd.DataFrame(rows, columns=["symbol", "entry_price", "shares", "stop_loss_price"])


class TestCheckStops:
    def test_no_alert_when_far_from_stop(self):
        pos = make_positions([("NVDA", 500.0, 10, 400.0)])
        alerts = check_stops(pos, {"NVDA": 490.0})  # 22.5% above stop — safe
        assert alerts == []

    def test_alert_within_5pct(self):
        pos = make_positions([("NVDA", 500.0, 10, 400.0)])
        alerts = check_stops(pos, {"NVDA": 418.0})  # 4.5% above stop → alert
        assert len(alerts) == 1
        assert alerts[0]["symbol"] == "NVDA"

    def test_alert_at_stop(self):
        pos = make_positions([("NVDA", 500.0, 10, 400.0)])
        alerts = check_stops(pos, {"NVDA": 400.0})  # exactly at stop
        assert len(alerts) == 1
        assert alerts[0]["distance_pct"] == pytest.approx(0.0)

    def test_alert_below_stop(self):
        pos = make_positions([("NVDA", 500.0, 10, 400.0)])
        alerts = check_stops(pos, {"NVDA": 380.0})  # breached
        assert len(alerts) == 1
        assert alerts[0]["distance_pct"] < 0

    def test_no_stop_set_is_skipped(self):
        pos = pd.DataFrame([{
            "symbol": "NVDA", "entry_price": 500.0, "shares": 10, "stop_loss_price": None
        }])
        alerts = check_stops(pos, {"NVDA": 400.0})
        assert alerts == []

    def test_missing_price_is_skipped(self):
        pos = make_positions([("NVDA", 500.0, 10, 400.0)])
        alerts = check_stops(pos, {})  # no price data
        assert alerts == []

    def test_multiple_positions_mixed(self):
        pos = make_positions([
            ("NVDA", 500.0, 10, 400.0),  # 490 price → 22.5% above → safe
            ("MSFT", 400.0, 5,  360.0),  # 362 price → 0.5% above → alert
            ("AMZN", 200.0, 20, 180.0),  # 200 price → 11% above → safe
        ])
        alerts = check_stops(pos, {"NVDA": 490.0, "MSFT": 362.0, "AMZN": 200.0})
        assert len(alerts) == 1
        assert alerts[0]["symbol"] == "MSFT"

    def test_empty_positions(self):
        alerts = check_stops(pd.DataFrame(), {"NVDA": 500.0})
        assert alerts == []


class TestFormatAlerts:
    def test_no_alerts(self):
        assert format_stop_alerts([]) == "No stop-loss alerts."

    def test_formats_alert(self):
        alerts = [{"symbol": "NVDA", "current_price": 410.0,
                   "stop_loss_price": 400.0, "distance_pct": 0.025}]
        msg = format_stop_alerts(alerts)
        assert "NVDA" in msg
        assert "410.00" in msg
        assert "400.00" in msg
