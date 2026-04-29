"""Streamlit dashboard v0.2 — macro / sector / stock signals + portfolio + editor."""

import sys
import os

# Allow running as `streamlit run dashboard/app.py` from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
    _AUTOREFRESH_AVAILABLE = True
except ImportError:
    _AUTOREFRESH_AVAILABLE = False

from config.settings import DASHBOARD_REFRESH_SECONDS, SIGNAL_HISTORY_DAYS, SQLITE_PATH
from config.universe import (
    BASE_CANDIDATE_TICKERS,
    BASE_CANDIDATES,
    TICKER_METADATA,
    UNCATEGORIZED_SECTOR,
    candidate_list_metadata,
    candidate_list_source,
    load_candidate_list,
    load_candidate_list_by_sector,
    load_research_universe,
    reset_candidate_list,
    save_candidate_list,
)
from portfolio.stoploss import check_stops
from portfolio.tracker import compute_pnl, get_open_positions

# Color palette from spec §7.2
COLOR = {
    "RISK_ON":      "#27AE60",
    "MODERATE_ON":  "#27AE60",
    "NEUTRAL":      "#F39C12",
    "MODERATE_OFF": "#E74C3C",
    "RISK_OFF":     "#E74C3C",
    "positive":     "#27AE60",
    "negative":     "#E74C3C",
    "info":         "#2E86C1",
    "inactive":     "#7F8C8D",
}

SIGNAL_LABELS = {
    "yield_10y_signal":    "10Y Yield",
    "vix_signal":          "VIX",
    "fed_bs_signal":       "Fed Balance Sheet",
    "credit_spread_signal": "Credit Spread (IG OAS)",
}


# ── Data loaders ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def load_regime_signals():
    try:
        from data.ingest.questdb_writer import query
        df = query(
            f"""
            SELECT ts, yield_10y_signal, vix_signal, fed_bs_signal,
                   credit_spread_signal, composite_score, regime
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
        from data.ingest.questdb_writer import query
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
        from data.ingest.questdb_writer import get_sector_signal_history
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
        from data.ingest.questdb_writer import get_stock_signal_history
        df = get_stock_signal_history(symbol, days)
        if not df.empty:
            df["ts"] = pd.to_datetime(df["ts"])
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_ohlcv_history(symbol: str, days: int = 180) -> pd.DataFrame:
    try:
        from data.ingest.questdb_writer import get_ohlcv_history
        df = get_ohlcv_history(symbol, days)
        if not df.empty:
            df["ts"] = pd.to_datetime(df["ts"])
        return df
    except Exception:
        return pd.DataFrame()


# ── Layout ────────────────────────────────────────────────────────────────────

def regime_color(regime: str) -> str:
    return COLOR.get(regime, COLOR["inactive"])


def signal_bar(score: int) -> str:
    """Return a simple ASCII bar for a score in [-2, +2]."""
    bars = {-2: "▓▓░░░", -1: "░▓▓░░", 0: "░░▓░░", 1: "░░▓▓░", 2: "░░▓▓▓"}
    return bars.get(score, "░░░░░")


def render_panel_a(signals_df: pd.DataFrame):
    st.subheader("A — Macro Regime")
    if signals_df.empty:
        st.info("No signal data available yet.")
        return

    latest = signals_df.iloc[-1]
    regime = latest.get("regime", "UNKNOWN")
    composite = int(latest.get("composite_score", 0))

    color = regime_color(regime)
    st.markdown(
        f"<h2 style='color:{color}; text-align:center'>{regime} &nbsp; ({composite:+d})</h2>",
        unsafe_allow_html=True,
    )

    cols = st.columns(4)
    for col, (key, label) in zip(cols, SIGNAL_LABELS.items()):
        score = int(latest.get(key, 0))
        col.metric(label, f"{score:+d}", delta_color="normal")
        col.text(signal_bar(score))


def render_panel_b(positions_df: pd.DataFrame, latest_prices: dict[str, float]):
    st.subheader("B — Portfolio Overview")
    if positions_df.empty:
        st.info("No open positions. Add positions via `portfolio/tracker.py`.")
        return

    df = compute_pnl(positions_df, latest_prices)
    display = df[["symbol", "entry_price", "current_price", "shares",
                  "pnl", "pnl_pct", "weight"]].copy()
    display = display.rename(columns={
        "entry_price": "Entry $",
        "current_price": "Current $",
        "pnl": "P&L $",
        "pnl_pct": "P&L %",
        "weight": "Weight %",
    })

    def color_pnl(val):
        if isinstance(val, float):
            c = COLOR["positive"] if val >= 0 else COLOR["negative"]
            return f"color: {c}"
        return ""

    styled = display.style.applymap(color_pnl, subset=["P&L $", "P&L %"])
    st.dataframe(styled, use_container_width=True)

    total_pnl = df["pnl"].sum() if "pnl" in df else 0
    pnl_color = COLOR["positive"] if total_pnl >= 0 else COLOR["negative"]
    st.markdown(
        f"**Total P&L:** <span style='color:{pnl_color}'>${total_pnl:,.2f}</span>",
        unsafe_allow_html=True,
    )


def render_panel_c(positions_df: pd.DataFrame, latest_prices: dict[str, float]):
    st.subheader("C — Stop-Loss Monitor")
    if positions_df.empty:
        st.info("No open positions.")
        return

    alerts = check_stops(positions_df, latest_prices)
    alert_symbols = {a["symbol"] for a in alerts}

    for _, row in positions_df.iterrows():
        symbol = row["symbol"]
        stop = row.get("stop_loss_price")
        current = latest_prices.get(symbol)

        if not stop or pd.isna(stop) or current is None:
            st.progress(0.0, text=f"{symbol}: no stop set")
            continue

        distance_pct = (current - stop) / stop
        # progress value: 0=at stop, 1=100% above stop; cap at 1
        progress = min(max(distance_pct / 0.20, 0.0), 1.0)

        is_alert = symbol in alert_symbols
        color_label = "🔴" if is_alert else "🟢"
        label = (
            f"{color_label} {symbol}: ${current:.2f} | stop ${stop:.2f} | "
            f"{distance_pct*100:.1f}% above stop"
        )
        st.progress(float(progress), text=label)


def render_panel_d(signals_df: pd.DataFrame):
    st.subheader("D — Signal History (60 days)")
    if signals_df.empty or len(signals_df) < 2:
        st.info("Not enough signal history yet.")
        return

    fig = go.Figure()

    # Regime zone shading
    zone_colors = {
        "RISK_ON":      "rgba(39,174,96,0.12)",
        "MODERATE_ON":  "rgba(39,174,96,0.06)",
        "NEUTRAL":      "rgba(243,156,18,0.10)",
        "MODERATE_OFF": "rgba(231,76,60,0.06)",
        "RISK_OFF":     "rgba(231,76,60,0.12)",
    }
    zone_thresholds = [
        ("RISK_ON", 5, 8),
        ("MODERATE_ON", 2, 4),
        ("NEUTRAL", -1, 1),
        ("MODERATE_OFF", -4, -2),
        ("RISK_OFF", -8, -5),
    ]
    x_min = signals_df["ts"].min()
    x_max = signals_df["ts"].max()
    for regime, y0, y1 in zone_thresholds:
        fig.add_hrect(
            y0=y0, y1=y1,
            fillcolor=zone_colors[regime],
            line_width=0,
            annotation_text=regime,
            annotation_position="right",
            annotation=dict(font_size=10, font_color=COLOR.get(regime, "#888")),
        )

    fig.add_trace(go.Scatter(
        x=signals_df["ts"],
        y=signals_df["composite_score"],
        mode="lines+markers",
        name="Composite Score",
        line=dict(color=COLOR["info"], width=2),
        marker=dict(size=5),
    ))

    fig.update_layout(
        yaxis=dict(title="Composite Score", range=[-8, 8], zeroline=True),
        xaxis=dict(title="Date"),
        height=350,
        margin=dict(l=40, r=80, t=20, b=40),
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)


def _sector_composite_color(score: int) -> str:
    """Map sector composite [-8..+8] to a regime-style color."""
    if score >= 5:   return COLOR["RISK_ON"]
    if score >= 2:   return COLOR["MODERATE_ON"]
    if score >= -1:  return COLOR["NEUTRAL"]
    if score >= -4:  return COLOR["MODERATE_OFF"]
    return COLOR["RISK_OFF"]


def render_panel_f(sector_df: pd.DataFrame):
    """Panel F — sector signal layer (Signal A SOX/SPX + B/C/D AI-infra)."""
    st.subheader("F — Sector Signals")
    if sector_df.empty:
        st.info(
            "No sector signals yet. Populate by running:\n\n"
            "`python scripts/daily_pipeline.py --step calc_sector_signals`"
        )
        return

    latest = sector_df.iloc[-1]
    composite = int(latest.get("sector_composite", 0))
    color = _sector_composite_color(composite)
    st.markdown(
        f"<h3 style='color:{color}; text-align:center'>"
        f"Sector Composite: {composite:+d}</h3>",
        unsafe_allow_html=True,
    )

    # Four signal tiles (A: SOX/SPX, B: TSMC, C: Capex, D: API pricing)
    tiles = [
        ("sox_spx_signal",    "A — SOX/SPX"),
        ("tsmc_signal",       "B — TSMC Revenue"),
        ("capex_signal",      "C — Hyperscaler CapEx"),
        ("api_pricing_signal", "D — AI API Pricing"),
    ]
    cols = st.columns(4)
    for col, (key, label) in zip(cols, tiles):
        score = int(latest.get(key, 0) or 0)
        col.metric(label, f"{score:+d}")
        col.text(signal_bar(score))

    # SOX/SPX ratio chart with EMA(20)/EMA(60) overlay
    if {"sox_spx_ratio", "sox_spx_ema20", "sox_spx_ema60"}.issubset(sector_df.columns):
        ratio_fig = go.Figure()
        ratio_fig.add_trace(go.Scatter(
            x=sector_df["ts"], y=sector_df["sox_spx_ratio"],
            name="SMH/SPY", mode="lines",
            line=dict(color=COLOR["info"], width=2),
        ))
        ratio_fig.add_trace(go.Scatter(
            x=sector_df["ts"], y=sector_df["sox_spx_ema20"],
            name="EMA(20)", mode="lines",
            line=dict(color=COLOR["positive"], width=1.5, dash="dash"),
        ))
        ratio_fig.add_trace(go.Scatter(
            x=sector_df["ts"], y=sector_df["sox_spx_ema60"],
            name="EMA(60)", mode="lines",
            line=dict(color=COLOR["negative"], width=1.5, dash="dot"),
        ))
        ratio_fig.update_layout(
            title="SMH / SPY ratio (Signal A — semi vs broad market)",
            yaxis_title="Ratio",
            xaxis_title="Date",
            height=320,
            margin=dict(l=40, r=20, t=40, b=40),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        )
        st.plotly_chart(ratio_fig, use_container_width=True)

    # Sector composite history with zone shading (mirrors Panel D)
    if "sector_composite" in sector_df.columns and len(sector_df) >= 2:
        comp_fig = go.Figure()
        zone_colors = {
            "RISK_ON":      "rgba(39,174,96,0.12)",
            "MODERATE_ON":  "rgba(39,174,96,0.06)",
            "NEUTRAL":      "rgba(243,156,18,0.10)",
            "MODERATE_OFF": "rgba(231,76,60,0.06)",
            "RISK_OFF":     "rgba(231,76,60,0.12)",
        }
        for regime, y0, y1 in [
            ("RISK_ON", 5, 8), ("MODERATE_ON", 2, 4),
            ("NEUTRAL", -1, 1), ("MODERATE_OFF", -4, -2), ("RISK_OFF", -8, -5),
        ]:
            comp_fig.add_hrect(
                y0=y0, y1=y1, fillcolor=zone_colors[regime], line_width=0,
                annotation_text=regime, annotation_position="right",
                annotation=dict(font_size=10, font_color=COLOR.get(regime, "#888")),
            )
        comp_fig.add_trace(go.Scatter(
            x=sector_df["ts"], y=sector_df["sector_composite"],
            mode="lines+markers", name="Sector Composite",
            line=dict(color=COLOR["info"], width=2),
            marker=dict(size=5),
        ))
        comp_fig.update_layout(
            yaxis=dict(title="Composite", range=[-8, 8], zeroline=True),
            xaxis=dict(title="Date"),
            height=300,
            margin=dict(l=40, r=80, t=20, b=40),
            showlegend=False,
        )
        st.plotly_chart(comp_fig, use_container_width=True)


def render_panel_g():
    """Panel G — stock technicals (per-ticker price + EMA + RSI + volume)."""
    st.subheader("G — Stock Technicals")

    grouped = load_candidate_list_by_sector()
    sectors = [s for s, tickers in grouped.items() if tickers]
    if not sectors:
        st.info("No candidates configured. Add some via Panel E.")
        return

    col_sec, col_sym, _ = st.columns([2, 2, 4])
    sector = col_sec.selectbox("Sector", sectors, key="panel_g_sector")
    tickers = sorted(grouped.get(sector, []))
    if not tickers:
        st.info(f"No tickers in sector '{sector}'.")
        return
    symbol = col_sym.selectbox("Ticker", tickers, key="panel_g_symbol")

    sig_df = load_stock_signal_history(symbol, days=180)
    ohlcv_df = load_ohlcv_history(symbol, days=180)

    if sig_df.empty:
        st.info(
            f"No stock signals yet for **{symbol}**. Run "
            "`python scripts/daily_pipeline.py --step calc_stock_signals` "
            "(needs ≥60 days of OHLCV history)."
        )
        return

    latest = sig_df.iloc[-1]
    composite = int(latest.get("stock_composite", 0) or 0)

    # Four signal tiles
    tiles = [
        ("ema_signal",    "EMA(20/60)"),
        ("rsi_signal",    "RSI(14)"),
        ("volume_signal", "Volume"),
        ("pead_signal",   "PEAD"),
    ]
    cols = st.columns(4)
    for col, (key, label) in zip(cols, tiles):
        score = int(latest.get(key, 0) or 0)
        col.metric(label, f"{score:+d}")
        col.text(signal_bar(score))

    # 3-subplot stacked chart: price+EMA / RSI / volume+MA
    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True,
        row_heights=[0.5, 0.25, 0.25],
        vertical_spacing=0.04,
        subplot_titles=("Price & EMA(20/60)", "RSI(14)", "Volume vs 20-day MA"),
    )

    # Row 1 — price + EMA(20) + EMA(60). Use OHLCV close + EMAs from sig_df.
    if not ohlcv_df.empty:
        fig.add_trace(go.Scatter(
            x=ohlcv_df["ts"], y=ohlcv_df["close"],
            name="Close", mode="lines",
            line=dict(color=COLOR["info"], width=1.5),
        ), row=1, col=1)
    if "ema_20" in sig_df.columns:
        fig.add_trace(go.Scatter(
            x=sig_df["ts"], y=sig_df["ema_20"],
            name="EMA(20)", mode="lines",
            line=dict(color=COLOR["positive"], width=1.2, dash="dash"),
        ), row=1, col=1)
    if "ema_60" in sig_df.columns:
        fig.add_trace(go.Scatter(
            x=sig_df["ts"], y=sig_df["ema_60"],
            name="EMA(60)", mode="lines",
            line=dict(color=COLOR["negative"], width=1.2, dash="dot"),
        ), row=1, col=1)

    # Row 2 — RSI with 30/70 reference lines
    if "rsi_14" in sig_df.columns:
        fig.add_trace(go.Scatter(
            x=sig_df["ts"], y=sig_df["rsi_14"],
            name="RSI(14)", mode="lines",
            line=dict(color=COLOR["info"], width=1.5),
            showlegend=False,
        ), row=2, col=1)
        fig.add_hline(y=70, line=dict(color=COLOR["negative"], width=1, dash="dash"),
                      row=2, col=1)
        fig.add_hline(y=30, line=dict(color=COLOR["positive"], width=1, dash="dash"),
                      row=2, col=1)
        fig.update_yaxes(range=[0, 100], row=2, col=1)

    # Row 3 — volume bars + 20-day MA line
    if not ohlcv_df.empty and "volume" in ohlcv_df.columns:
        fig.add_trace(go.Bar(
            x=ohlcv_df["ts"], y=ohlcv_df["volume"],
            name="Volume", marker=dict(color=COLOR["inactive"]),
            showlegend=False,
        ), row=3, col=1)
        vol_ma = ohlcv_df["volume"].rolling(20).mean()
        fig.add_trace(go.Scatter(
            x=ohlcv_df["ts"], y=vol_ma,
            name="Vol MA(20)", mode="lines",
            line=dict(color=COLOR["info"], width=1.5),
            showlegend=False,
        ), row=3, col=1)

    fig.update_layout(
        height=620,
        margin=dict(l=40, r=20, t=40, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.04, x=0),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Composite footer
    comp_color = _sector_composite_color(composite)  # reuse same band mapping
    st.markdown(
        f"<div style='text-align:center'>"
        f"<b>Stock composite for {symbol}: "
        f"<span style='color:{comp_color}'>{composite:+d}</span></b> "
        f"&nbsp;(range −7 … +7)</div>",
        unsafe_allow_html=True,
    )


def render_panel_e_candidate_editor():
    """Panel E — sector-grouped candidate list editor.

    Each sector gets its own multiselect, so users can curate per-sector
    candidate sets that drive different signal logic downstream. Backs onto
    the same `config/candidate_list.json` used by the CLI
    (`scripts/manage_candidates.py`) and the daily pipeline. All three paths
    write through `save_candidate_list()` so they stay in sync.
    """
    st.subheader("E — Candidate List Editor (by sector)")

    grouped = load_candidate_list_by_sector()
    flat_total = sum(len(t) for t in grouped.values())
    source = candidate_list_source()
    meta = candidate_list_metadata()

    # Source pool: prefer the research universe (so users only pick tickers
    # they have OHLCV for); fall back to BASE + current.
    research = load_research_universe()
    current_flat = {t for tickers in grouped.values() for t in tickers}
    pool = sorted(set(research) | current_flat | set(BASE_CANDIDATE_TICKERS))

    info_cols = st.columns([2, 2, 3])
    info_cols[0].metric("Candidates", flat_total, f"{len(grouped)} sectors")
    info_cols[1].markdown(f"**Source**\n\n`{source}`")
    if meta.get("updated_at"):
        info_cols[2].markdown(f"**Last updated**\n\n{meta.get('updated_at', '—')}")

    if meta.get("notes") or meta.get("note"):
        st.caption(f"📝 Latest note: _{meta.get('notes') or meta.get('note')}_")

    with st.expander("✏️ Edit candidate list (per sector)", expanded=False):
        st.caption(
            "Each sector drives different signal logic. A ticker can only live "
            "in one sector at a time — the **last** sector you put it in wins."
        )

        # Order: known seed sectors first (stable order), then any custom
        # sectors the user has created, then uncategorized at the bottom.
        ordered_sectors: list[str] = []
        for s in BASE_CANDIDATES.keys():
            ordered_sectors.append(s)
        for s in grouped.keys():
            if s not in ordered_sectors and s != UNCATEGORIZED_SECTOR:
                ordered_sectors.append(s)
        if UNCATEGORIZED_SECTOR in grouped or any(
            s == UNCATEGORIZED_SECTOR for s in grouped
        ):
            ordered_sectors.append(UNCATEGORIZED_SECTOR)

        # Per-sector multiselects → collected into edits dict
        edits: dict[str, list[str]] = {}
        for sector in ordered_sectors:
            current = grouped.get(sector, [])
            edits[sector] = st.multiselect(
                f"**{sector}** ({len(current)})",
                options=pool,
                default=current,
                key=f"candidate_editor_{sector}",
                help=f"Tickers in '{sector}'. Picking a ticker that's currently "
                     "in another sector will move it on save.",
            )

        # Optional new sector creator
        if st.checkbox("➕ Add a new sector", key="candidate_show_new_sector"):
            new_sector_name = st.text_input(
                "New sector name",
                placeholder="e.g. 'cybersecurity' or 'energy_storage'",
                key="candidate_new_sector_name",
            )
            new_sector_tickers = st.multiselect(
                "Tickers in the new sector",
                options=pool,
                default=[],
                key="candidate_new_sector_tickers",
            )
            if new_sector_name and new_sector_tickers:
                edits[new_sector_name.strip().lower().replace(" ", "_")] = new_sector_tickers

        note = st.text_input(
            "Note (optional)",
            placeholder="e.g. 'Q2 rebalance: moved CRWD to new cybersecurity sector'",
            key="candidate_editor_note",
        )

        col_save, col_reset, _ = st.columns([1, 1, 4])
        if col_save.button("💾 Save", type="primary", key="candidate_save"):
            # Resolve duplicates: if a ticker appears in multiple sectors, keep
            # only the LAST occurrence (in `edits` insertion order). This is
            # what users will expect when they re-assign a ticker.
            resolved: dict[str, list[str]] = {}
            seen: dict[str, str] = {}
            for sector, tickers in reversed(list(edits.items())):
                kept = []
                for t in tickers:
                    if t in seen:
                        continue  # already claimed by a later sector
                    seen[t] = sector
                    kept.append(t)
                if kept:
                    resolved[sector] = sorted(kept)
            # Restore original sector order
            ordered_resolved = {s: resolved[s] for s in edits.keys() if s in resolved}

            if not any(ordered_resolved.values()):
                st.warning("At least one ticker is required — refusing to save empty list.")
            else:
                path = save_candidate_list(ordered_resolved, note=note)
                total = sum(len(t) for t in ordered_resolved.values())
                st.toast(
                    f"Saved {total} candidates across {len(ordered_resolved)} "
                    f"sectors → {path.name}",
                    icon="✅",
                )
                st.cache_data.clear()
                st.rerun()

        confirm_key = "candidate_reset_confirm"
        if col_reset.button("↺ Reset to defaults", key="candidate_reset_btn"):
            st.session_state[confirm_key] = True

        if st.session_state.get(confirm_key):
            st.warning(
                f"Reset will delete `candidate_list.json` and revert to the "
                f"{len(BASE_CANDIDATE_TICKERS)} BASE_CANDIDATES seed across "
                f"{len(BASE_CANDIDATES)} sectors. Click **Confirm reset** to proceed."
            )
            if st.button("Confirm reset", key="candidate_reset_confirm_btn"):
                deleted = reset_candidate_list()
                st.session_state[confirm_key] = False
                if deleted:
                    st.toast("Reset to BASE_CANDIDATES", icon="✅")
                else:
                    st.toast("Already on BASE_CANDIDATES — nothing to reset", icon="ℹ️")
                st.cache_data.clear()
                st.rerun()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(
        page_title="Quantamental Dashboard",
        page_icon="📊",
        layout="wide",
    )

    # Non-blocking auto-refresh — registers a JS timer that triggers st.rerun()
    # without freezing the UI thread. Falls back to no auto-refresh if the
    # streamlit-autorefresh package isn't installed (manual reload still works).
    if _AUTOREFRESH_AVAILABLE:
        st_autorefresh(
            interval=DASHBOARD_REFRESH_SECONDS * 1000,  # ms
            key="dashboard_refresh",
        )
        refresh_caption = f"Auto-refreshes every {DASHBOARD_REFRESH_SECONDS}s"
    else:
        refresh_caption = (
            f"Manual refresh only — install `streamlit-autorefresh` for live updates"
        )

    st.title("📊 Quantamental Dashboard — Month 2")
    st.caption(refresh_caption)

    signals_df = load_regime_signals()
    sector_df = load_sector_signals(days=90)
    latest_prices = load_latest_prices()
    positions_df = get_open_positions(SQLITE_PATH)

    # Layered analysis story (top → bottom): macro → sector → stock,
    # then portfolio + ops, then candidate editor as the control surface.
    render_panel_a(signals_df)
    st.divider()
    render_panel_f(sector_df)
    st.divider()
    render_panel_g()
    st.divider()
    render_panel_b(positions_df, latest_prices)
    st.divider()
    render_panel_c(positions_df, latest_prices)
    st.divider()
    render_panel_d(signals_df)
    st.divider()
    render_panel_e_candidate_editor()


if __name__ == "__main__":
    main()
