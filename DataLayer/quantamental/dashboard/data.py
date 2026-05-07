import pandas as pd
import streamlit as st

from quantamental.config.settings import SIGNAL_HISTORY_DAYS


@st.cache_data(ttl=60)
def load_regime_signals():
    try:
        from quantamental.data.ingest.questdb_writer import query

        df = query(
            f"""
            SELECT ts, yield_10y_signal, vix_signal, fed_bs_signal,
                   credit_spread_signal, composite_score, regime, confirmed_regime
            FROM regime_signals
            ORDER BY ts DESC
            LIMIT {SIGNAL_HISTORY_DAYS}
            """
        )
        df["ts"] = pd.to_datetime(df["ts"])
        return df.sort_values("ts")
    except Exception as e:
        st.warning(f"QuestDB unavailable: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_latest_prices() -> dict[str, float]:
    try:
        from quantamental.data.ingest.questdb_writer import query

        df = query(
            """
            SELECT symbol, close
            FROM daily_ohlcv
            LATEST ON ts PARTITION BY symbol
            """
        )
        return dict(zip(df["symbol"], df["close"]))
    except Exception:
        return {}


@st.cache_data(ttl=60)
def load_sector_signals(days: int = 90) -> pd.DataFrame:
    try:
        from quantamental.data.ingest.questdb_writer import get_sector_signal_history

        df = get_sector_signal_history(days)
        if not df.empty:
            df["ts"] = pd.to_datetime(df["ts"])
        return df
    except Exception as exc:
        st.warning(f"sector_signals query failed: {exc}")
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_stock_signal_history(symbol: str, days: int = 180) -> pd.DataFrame:
    try:
        from quantamental.data.ingest.questdb_writer import get_stock_signal_history

        df = get_stock_signal_history(symbol, days)
        if not df.empty:
            df["ts"] = pd.to_datetime(df["ts"])
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_ohlcv_history(symbol: str, days: int = 180) -> pd.DataFrame:
    try:
        from quantamental.data.ingest.questdb_writer import get_ohlcv_history

        df = get_ohlcv_history(symbol, days)
        if not df.empty:
            df["ts"] = pd.to_datetime(df["ts"])
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_latest_alpha_ranks() -> pd.DataFrame:
    try:
        from quantamental.alpha.reporting import load_latest_alpha_ranks as _load

        return _load()
    except Exception as exc:
        st.warning(f"Alpha ranks unavailable: {exc}")
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_latest_alpha_performance() -> dict[str, pd.DataFrame]:
    try:
        from quantamental.alpha.reporting import load_latest_alpha_performance as _load

        return _load()
    except Exception as exc:
        st.warning(f"Alpha performance unavailable: {exc}")
        return {"headline": pd.DataFrame(), "bucket_summary": pd.DataFrame()}


@st.cache_data(ttl=60)
def load_data_freshness() -> dict:
    try:
        from quantamental.alpha.reporting import load_latest_alpha_ranks as _load_ranks
        from quantamental.dashboard.freshness import build_freshness_report

        return build_freshness_report(alpha_ranks=_load_ranks())
    except Exception as exc:
        st.warning(f"Data freshness unavailable: {exc}")
        return {
            "status": "BLOCKED",
            "trusted": False,
            "checks": [
                {
                    "component": "Freshness",
                    "status": "FAIL",
                    "latest_date": None,
                    "expected_date": None,
                    "lag_days": None,
                    "detail": str(exc),
                    "fix": "docker compose up -d",
                }
            ],
        }
