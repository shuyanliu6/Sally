"""
Unit tests for portfolio tracker and trade journal.
Uses a temp SQLite file — no QuestDB needed.
"""

import sys, os, tempfile, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
from portfolio.tracker import init_db, add_position, close_position, get_open_positions, compute_pnl
from portfolio.journal import log_trade, get_recent, fill_30d_review, get_all


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


class TestTracker:
    def test_init_creates_tables(self, db):
        df = get_open_positions(db)
        assert list(df.columns)  # just confirms it runs without error

    def test_add_and_retrieve_position(self, db):
        add_position("NVDA", "2024-01-15", 500.0, 10, target_weight=0.05,
                     stop_loss_price=450.0, thesis="AI compute cycle", path=db)
        df = get_open_positions(db)
        assert len(df) == 1
        assert df.iloc[0]["symbol"] == "NVDA"
        assert df.iloc[0]["status"] == "OPEN"

    def test_close_position(self, db):
        pos_id = add_position("MSFT", "2024-01-15", 400.0, 5, path=db)
        close_position(pos_id, path=db)
        df = get_open_positions(db)
        assert len(df) == 0  # closed positions not returned

    def test_multiple_positions(self, db):
        add_position("NVDA", "2024-01-01", 500.0, 10, path=db)
        add_position("MSFT", "2024-01-02", 400.0, 5, path=db)
        add_position("AMZN", "2024-01-03", 180.0, 20, path=db)
        df = get_open_positions(db)
        assert len(df) == 3

    def test_compute_pnl(self, db):
        add_position("NVDA", "2024-01-01", 500.0, 10,
                     stop_loss_price=450.0, path=db)
        add_position("MSFT", "2024-01-01", 400.0, 5,
                     stop_loss_price=360.0, path=db)
        positions = get_open_positions(db)
        prices = {"NVDA": 550.0, "MSFT": 380.0}  # NVDA up, MSFT down
        result = compute_pnl(positions, prices)

        nvda = result[result["symbol"] == "NVDA"].iloc[0]
        msft = result[result["symbol"] == "MSFT"].iloc[0]

        assert nvda["pnl"] == pytest.approx(500.0)   # (550-500)*10
        assert nvda["pnl_pct"] == pytest.approx(10.0)
        assert msft["pnl"] == pytest.approx(-100.0)  # (380-400)*5
        assert msft["pnl_pct"] == pytest.approx(-5.0)

    def test_compute_pnl_weight(self, db):
        add_position("NVDA", "2024-01-01", 100.0, 1, path=db)  # MV=100
        add_position("MSFT", "2024-01-01", 100.0, 3, path=db)  # MV=300
        positions = get_open_positions(db)
        result = compute_pnl(positions, {"NVDA": 100.0, "MSFT": 100.0})
        # Total MV = 400; NVDA=25%, MSFT=75%
        nvda_w = result[result["symbol"] == "NVDA"].iloc[0]["weight"]
        msft_w = result[result["symbol"] == "MSFT"].iloc[0]["weight"]
        assert nvda_w == pytest.approx(25.0)
        assert msft_w == pytest.approx(75.0)


class TestJournal:
    def test_log_and_retrieve(self, db):
        log_trade("NVDA", "BUY", quantity=10, price=500.0,
                  trigger_reason="Regime RISK_ON", emotion="Confident",
                  thesis_still_valid="YES", path=db)
        df = get_recent(path=db)
        assert len(df) == 1
        assert df.iloc[0]["symbol"] == "NVDA"
        assert df.iloc[0]["action"] == "BUY"

    def test_invalid_action_raises(self, db):
        with pytest.raises(ValueError):
            log_trade("NVDA", "HOLD", path=db)

    def test_invalid_thesis_raises(self, db):
        with pytest.raises(ValueError):
            log_trade("NVDA", "BUY", thesis_still_valid="MAYBE", path=db)

    def test_fill_30d_review(self, db):
        entry_id = log_trade("NVDA", "BUY", quantity=10, price=500.0, path=db)
        fill_30d_review(entry_id, "Thesis intact, held position", path=db)
        df = get_all(path=db)
        assert df.iloc[0]["review_30d"] == "Thesis intact, held position"

    def test_get_recent_limit(self, db):
        for i in range(25):
            log_trade("SPY", "BUY", price=float(400 + i), path=db)
        df = get_recent(n=10, path=db)
        assert len(df) == 10
