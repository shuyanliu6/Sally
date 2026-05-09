import numpy as np
import pandas as pd

from quantamental.alpha.backtest import run_backtest
from quantamental.alpha.diagnostics import build_alpha_diagnostic_report
from quantamental.alpha.features import add_forward_returns, build_features
from quantamental.alpha.performance import build_performance_report
from quantamental.alpha.portfolio import construct_portfolio
from quantamental.alpha.ranking import rank_alpha
from quantamental.signals.earnings import active_pead_events, load_earnings_events, log_earnings_event


def _ohlcv(symbols=("AAA", "BBB", "CCC", "SMH", "SPY", "QQQ"), periods=80):
    dates = pd.date_range("2024-01-01", periods=periods, freq="B")
    rows = []
    for symbol in symbols:
        if symbol == "AAA":
            close = np.linspace(100, 150, periods)
        elif symbol == "BBB":
            close = np.linspace(100, 115, periods)
        elif symbol == "CCC":
            close = np.linspace(100, 80, periods)
        elif symbol == "SMH":
            close = np.linspace(100, 120, periods)
        elif symbol == "QQQ":
            close = np.linspace(100, 116, periods)
        else:
            close = np.linspace(100, 110, periods)
        for ts, px in zip(dates, close, strict=False):
            rows.append(
                {
                    "symbol": symbol,
                    "ts": ts,
                    "open": px,
                    "high": px,
                    "low": px,
                    "close": px,
                    "volume": 1_000_000,
                }
            )
    return pd.DataFrame(rows)


def _stock_signals(asof="2024-03-01"):
    return pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "ts": pd.Timestamp(asof),
                "ema_signal": 2,
                "rsi_signal": 1,
                "volume_signal": 1,
                "pead_signal": 1,
                "stock_composite": 6,
                "rsi_14": 55,
                "volume_ratio": 2.0,
            },
            {
                "symbol": "BBB",
                "ts": pd.Timestamp(asof),
                "ema_signal": 0,
                "rsi_signal": 0,
                "volume_signal": 0,
                "pead_signal": 0,
                "stock_composite": 0,
                "rsi_14": 50,
                "volume_ratio": 1.0,
            },
            {
                "symbol": "CCC",
                "ts": pd.Timestamp(asof),
                "ema_signal": -2,
                "rsi_signal": -1,
                "volume_signal": -1,
                "pead_signal": -1,
                "stock_composite": -6,
                "rsi_14": 75,
                "volume_ratio": 2.0,
            },
        ]
    )


def _regime(score=4, regime="MODERATE_ON"):
    return pd.DataFrame(
        [{"ts": pd.Timestamp("2024-03-01"), "composite_score": score, "confirmed_regime": regime}]
    )


def _sector(score=3):
    return pd.DataFrame(
        [{"ts": pd.Timestamp("2024-03-01"), "sector_composite": score, "sox_spx_signal": 1}]
    )


def test_features_do_not_use_rows_after_asof():
    ohlcv = _ohlcv(symbols=("AAA", "SMH"), periods=50)
    future = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "ts": pd.Timestamp("2024-04-30"),
                "open": 999,
                "high": 999,
                "low": 999,
                "close": 999,
                "volume": 1,
            }
        ]
    )
    ohlcv = pd.concat([ohlcv, future], ignore_index=True)
    stock = pd.concat(
        [
            _stock_signals("2024-02-15").head(1),
            pd.DataFrame([{"symbol": "AAA", "ts": pd.Timestamp("2024-04-30"), "stock_composite": -7}]),
        ],
        ignore_index=True,
    )
    features = build_features(
        ohlcv=ohlcv,
        stock_signals=stock,
        regime_signals=_regime(),
        sector_signals=_sector(),
        symbols=["AAA"],
        asof="2024-03-01",
    )
    row = features.iloc[0]
    assert row["close"] != 999
    assert row["stock_composite"] == 5


def test_features_fallback_computes_technical_stock_signals_from_ohlcv():
    features = build_features(
        ohlcv=_ohlcv(periods=80),
        stock_signals=pd.DataFrame(),
        regime_signals=_regime(),
        sector_signals=_sector(),
        symbols=["AAA", "CCC"],
        asof="2024-04-19",
    )

    by_symbol = features.set_index("symbol")
    assert by_symbol.at["AAA", "ema_signal"] > 0
    assert by_symbol.at["CCC", "ema_signal"] < 0
    assert features["ema_signal"].nunique() > 1
    assert features["rsi_signal"].nunique() > 1


def test_features_apply_pead_from_point_in_time_earnings_events():
    events = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "report_date": "2024-04-01",
                "surprise_pct": 15.0,
            },
            {
                "symbol": "CCC",
                "report_date": "2024-05-01",
                "surprise_pct": -15.0,
            },
        ]
    )
    features = build_features(
        ohlcv=_ohlcv(periods=90),
        stock_signals=pd.DataFrame(),
        regime_signals=_regime(),
        sector_signals=_sector(),
        earnings_events=events,
        symbols=["AAA", "CCC"],
        asof="2024-04-03",
    )
    by_symbol = features.set_index("symbol")
    assert by_symbol.at["AAA", "pead_signal"] > 0
    assert by_symbol.at["CCC", "pead_signal"] == 0

    expired = build_features(
        ohlcv=_ohlcv(periods=120),
        stock_signals=pd.DataFrame(),
        regime_signals=_regime(),
        sector_signals=_sector(),
        earnings_events=events,
        symbols=["AAA"],
        asof="2024-05-20",
    )
    assert expired.iloc[0]["pead_signal"] == 0


def test_earnings_event_log_derives_and_loads_surprise(tmp_path):
    db_path = tmp_path / "meta.db"
    event_id = log_earnings_event(
        symbol="aaa",
        report_date="2024-04-01",
        eps_actual=1.20,
        eps_estimate=1.00,
        fiscal_period="2024-Q1",
        path=str(db_path),
    )
    events = load_earnings_events(symbols=["AAA"], path=str(db_path))
    assert event_id == 1
    assert events.iloc[0]["symbol"] == "AAA"
    assert round(float(events.iloc[0]["surprise_pct"]), 1) == 20.0


def test_active_pead_events_filters_window_and_scores(tmp_path):
    db_path = tmp_path / "meta.db"
    log_earnings_event("AAA", "2024-04-01", surprise_pct=15.0, path=str(db_path))
    log_earnings_event("BBB", "2024-03-01", surprise_pct=15.0, path=str(db_path))
    log_earnings_event("CCC", "2024-04-05", surprise_pct=-15.0, path=str(db_path))

    active = active_pead_events("2024-04-10", symbols=["AAA", "BBB", "CCC"], path=str(db_path))

    assert set(active["symbol"]) == {"AAA", "CCC"}
    by_symbol = active.set_index("symbol")
    assert by_symbol.at["AAA", "pead_signal"] > 0
    assert by_symbol.at["CCC", "pead_signal"] < 0
    assert by_symbol.at["AAA", "days_remaining"] == 19


def test_features_include_full_asof_date_and_keep_price_close():
    ohlcv = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "ts": pd.Timestamp("2024-03-01 20:00:00"),
                "open": 123,
                "high": 123,
                "low": 123,
                "close": 123,
                "volume": 1_000_000,
            },
            {
                "symbol": "SMH",
                "ts": pd.Timestamp("2024-03-01 20:00:00"),
                "open": 100,
                "high": 100,
                "low": 100,
                "close": 100,
                "volume": 1_000_000,
            },
        ]
    )
    stock = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "ts": pd.Timestamp("2024-03-01 20:00:00"),
                "close": 0,
                "ema_signal": 1,
                "rsi_signal": 0,
                "volume_signal": 0,
                "pead_signal": 0,
                "stock_composite": 1,
            }
        ]
    )
    features = build_features(
        ohlcv=ohlcv,
        stock_signals=stock,
        regime_signals=_regime(),
        sector_signals=_sector(),
        symbols=["AAA"],
        asof="2024-03-01",
    )
    row = features.iloc[0]
    assert row["close"] == 123
    assert row["price_asof_date"] == "2024-03-01"


def test_forward_returns_use_next_close_after_signal_date():
    ohlcv = pd.DataFrame(
        [
            ("AAA", "2024-01-01", 100),
            ("AAA", "2024-01-02", 110),
            ("AAA", "2024-01-03", 121),
            ("AAA", "2024-01-04", 133.1),
            ("SMH", "2024-01-01", 100),
            ("SMH", "2024-01-02", 100),
            ("SMH", "2024-01-03", 100),
            ("SMH", "2024-01-04", 100),
        ],
        columns=["symbol", "ts", "close"],
    )
    ohlcv["ts"] = pd.to_datetime(ohlcv["ts"])
    features = pd.DataFrame([{"symbol": "AAA", "asof_date": "2024-01-01"}])
    labeled = add_forward_returns(features, ohlcv, horizons=(2,), benchmark="SMH")
    assert round(float(labeled["fwd_2d_return"].iloc[0]), 3) == 0.210
    assert round(float(labeled["fwd_2d_excess_SMH"].iloc[0]), 3) == 0.210


def test_ranking_is_deterministic_and_explainable():
    features = build_features(
        ohlcv=_ohlcv(),
        stock_signals=_stock_signals(),
        regime_signals=_regime(),
        sector_signals=_sector(),
        symbols=["AAA", "BBB", "CCC"],
        asof="2024-03-01",
    )
    ranks = rank_alpha(features)
    assert ranks.iloc[0]["symbol"] == "AAA"
    assert ranks.iloc[-1]["symbol"] == "CCC"
    assert "score_components" in ranks.columns
    assert ranks["rank"].tolist() == [1, 2, 3]


def test_disabled_components_are_tracked_but_not_scored_in_v1():
    ranks = rank_alpha(
        pd.DataFrame(
            [
                {"symbol": "LOW", "volatility_20": 0.10, "rsi_signal": -2},
                {"symbol": "HIGH", "volatility_20": 1.00, "rsi_signal": 2},
            ]
        )
    )

    assert "volatility_20_component" in ranks.columns
    assert "rsi_signal_component" in ranks.columns
    assert ranks["volatility_20_component"].abs().sum() == 0
    assert ranks["rsi_signal_component"].abs().sum() == 0
    assert ranks["alpha_score"].nunique() == 1


def test_stale_price_candidate_is_forced_to_avoid():
    ohlcv = _ohlcv(symbols=("AAA", "BBB", "SMH"), periods=80)
    ohlcv = ohlcv[
        ~(
            ohlcv["symbol"].eq("AAA")
            & (ohlcv["ts"] > pd.Timestamp("2024-02-15"))
        )
    ]
    features = build_features(
        ohlcv=ohlcv,
        stock_signals=_stock_signals(),
        regime_signals=_regime(),
        sector_signals=_sector(),
        symbols=["AAA", "BBB"],
        asof="2024-03-01",
    )
    ranks = rank_alpha(features)
    stale = ranks[ranks["symbol"].eq("AAA")].iloc[0]
    assert stale["data_quality_flag"] == "STALE_PRICE"
    assert stale["bucket"] == "AVOID"
    assert "data_quality_component" in stale["score_components"]


def test_portfolio_risk_off_blocks_new_buys_and_keeps_cash():
    features = build_features(
        ohlcv=_ohlcv(),
        stock_signals=_stock_signals(),
        regime_signals=_regime(score=-8, regime="RISK_OFF"),
        sector_signals=_sector(score=3),
        symbols=["AAA", "BBB", "CCC"],
        asof="2024-03-01",
    )
    portfolio = construct_portfolio(rank_alpha(features), top_n=10)
    assert portfolio["target_weight"].sum() <= 0.50
    assert portfolio["target_cash"].iloc[0] >= 0.50
    assert portfolio["new_buys_allowed"].iloc[0] is False or portfolio["new_buys_allowed"].iloc[0] == 0


def test_backtest_outputs_baselines_and_costs_reduce_returns():
    ohlcv = _ohlcv(periods=90)
    stock = _stock_signals("2024-01-10")
    # Repeat stock signals so every rebalance can see the intended ordering.
    stock = pd.concat(
        [
            stock.assign(ts=pd.Timestamp("2024-01-10")),
            stock.assign(ts=pd.Timestamp("2024-02-15")),
            stock.assign(ts=pd.Timestamp("2024-03-15")),
        ],
        ignore_index=True,
    )
    no_cost = run_backtest(
        ohlcv=ohlcv,
        stock_signals=stock,
        regime_signals=_regime(),
        sector_signals=_sector(),
        symbols=["AAA", "BBB", "CCC"],
        start="2024-02-01",
        end="2024-04-15",
        transaction_cost_bps=0,
    )
    high_cost = run_backtest(
        ohlcv=ohlcv,
        stock_signals=stock,
        regime_signals=_regime(),
        sector_signals=_sector(),
        symbols=["AAA", "BBB", "CCC"],
        start="2024-02-01",
        end="2024-04-15",
        transaction_cost_bps=100,
    )
    assert {"alpha_strategy", "equal_weight_candidates", "SPY", "QQQ", "SMH"}.issubset(
        set(no_cost.metrics["strategy"])
    )
    assert no_cost.rebalance_log.groupby("asof_date").first()["symbol"].iloc[0] == "AAA"
    assert high_cost.daily_returns["alpha_strategy"].sum() < no_cost.daily_returns["alpha_strategy"].sum()


def test_performance_report_summarizes_bucket_forward_excess():
    ohlcv = _ohlcv(periods=95)
    stock = pd.concat(
        [
            _stock_signals("2024-01-10"),
            _stock_signals("2024-02-01"),
            _stock_signals("2024-03-01"),
        ],
        ignore_index=True,
    )
    report = build_performance_report(
        ohlcv=ohlcv,
        stock_signals=stock,
        regime_signals=_regime(),
        sector_signals=_sector(),
        symbols=["AAA", "BBB", "CCC"],
        start="2024-02-01",
        end="2024-03-15",
        horizons=(20, 40),
        frequency="weekly",
    )

    assert not report.rank_log.empty
    assert {"TOP_BUY", "BUY", "HOLD", "AVOID"} & set(report.bucket_summary["bucket"])
    assert {20, 40}.issubset(set(report.bucket_summary["horizon"]))
    assert "top_minus_avoid" in report.headline.columns

    h20 = report.headline[report.headline["horizon"].eq(20)].iloc[0]
    assert h20["top_buy_buy_avg_excess"] > h20["avoid_avg_excess"]


def test_alpha_diagnostics_identify_predictive_component():
    ohlcv = _ohlcv(periods=95)
    stock = pd.concat(
        [
            _stock_signals("2024-01-10"),
            _stock_signals("2024-02-01"),
            _stock_signals("2024-03-01"),
        ],
        ignore_index=True,
    )
    performance = build_performance_report(
        ohlcv=ohlcv,
        stock_signals=stock,
        regime_signals=_regime(),
        sector_signals=_sector(),
        symbols=["AAA", "BBB", "CCC"],
        start="2024-02-01",
        end="2024-03-15",
        horizons=(20,),
        frequency="weekly",
    )

    diagnostics = build_alpha_diagnostic_report(
        performance.rank_log,
        horizons=(20,),
        quantile=1 / 3,
        min_dates=1,
    )

    assert not diagnostics.component_summary.empty
    alpha_row = diagnostics.component_summary[
        diagnostics.component_summary["factor"].eq("alpha_score")
    ].iloc[0]
    assert alpha_row["mean_rank_ic"] > 0
    assert alpha_row["top_minus_bottom"] > 0
    assert alpha_row["recommendation"] == "SUPPORTIVE"
    assert not diagnostics.bucket_attribution.empty
