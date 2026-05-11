"""Streamlit dashboard entrypoint."""

if __package__ in (None, ""):
    from _bootstrap import add_project_root

    add_project_root(__file__)

import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh

    _AUTOREFRESH_AVAILABLE = True
except ImportError:
    _AUTOREFRESH_AVAILABLE = False

from quantamental.config.settings import DASHBOARD_REFRESH_SECONDS, SQLITE_PATH
from quantamental.dashboard.data import (
    load_active_pead_events,
    load_alpha_rank_artifact_info,
    load_data_freshness,
    load_latest_alpha_performance,
    load_latest_alpha_ranks,
    load_latest_prices,
    load_regime_signals,
    load_sector_signals,
)
from quantamental.dashboard.panels import (
    render_panel_a,
    render_panel_b,
    render_panel_c,
    render_panel_d,
    render_panel_e_candidate_editor,
    render_panel_f,
    render_panel_g,
    render_panel_h_alpha,
    render_panel_i_alpha_validation,
    render_panel_j_pead_events,
    render_panel_k_etfs,
    render_portfolio_risk,
    render_dashboard_clock,
    render_data_freshness_gate,
    render_overview,
)
from quantamental.dashboard.ui import apply_global_styles
from quantamental.portfolio.tracker import get_open_positions


def main():
    st.set_page_config(
        page_title="Quantamental",
        page_icon="Q",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    apply_global_styles()

    if _AUTOREFRESH_AVAILABLE:
        st_autorefresh(
            interval=DASHBOARD_REFRESH_SECONDS * 1000,
            key="dashboard_refresh",
        )
        refresh_caption = f"Refresh {DASHBOARD_REFRESH_SECONDS}s"
    else:
        refresh_caption = "Manual refresh"

    st.title("Quantamental")
    st.caption(f"AI-infra alpha, regime, risk, and portfolio dashboard | {refresh_caption}")

    signals_df = load_regime_signals()
    sector_df = load_sector_signals(days=90)
    alpha_ranks = load_latest_alpha_ranks()
    alpha_artifact = load_alpha_rank_artifact_info()
    alpha_asof = (
        str(alpha_ranks["asof_date"].iloc[0])
        if not alpha_ranks.empty and "asof_date" in alpha_ranks
        else None
    )
    pead_events = load_active_pead_events(alpha_asof)
    alpha_performance = load_latest_alpha_performance()
    freshness = load_data_freshness()
    latest_prices = load_latest_prices()
    positions_df = get_open_positions(SQLITE_PATH)

    render_dashboard_clock(freshness)

    overview_tab, alpha_tab, signals_tab, etf_tab, portfolio_tab, universe_tab = st.tabs(
        ["Overview", "Alpha", "Signals", "ETFs", "Portfolio", "Universe"]
    )

    with overview_tab:
        render_overview(signals_df, sector_df, alpha_ranks, positions_df, latest_prices, freshness)

    with alpha_tab:
        render_data_freshness_gate(freshness)
        render_panel_h_alpha(alpha_ranks, artifact_info=alpha_artifact)
        render_panel_j_pead_events(pead_events, alpha_asof)
        render_panel_i_alpha_validation(alpha_performance)

    with signals_tab:
        render_panel_a(signals_df)
        render_panel_f(sector_df)
        render_panel_g()
        render_panel_d(signals_df)

    with etf_tab:
        render_panel_k_etfs(latest_prices)

    with portfolio_tab:
        render_portfolio_risk(alpha_ranks, positions_df, latest_prices)
        render_panel_b(positions_df, latest_prices)
        render_panel_c(positions_df, latest_prices)

    with universe_tab:
        render_panel_e_candidate_editor()


if __name__ == "__main__":
    main()
