import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from html import escape

from quantamental.config.universe import (
    BASE_CANDIDATE_TICKERS,
    BASE_CANDIDATES,
    UNCATEGORIZED_SECTOR,
    candidate_list_metadata,
    candidate_list_source,
    load_candidate_list_by_sector,
    load_research_universe,
    reset_candidate_list,
    save_candidate_list,
)
from quantamental.dashboard.data import load_latest_alpha_ranks, load_ohlcv_history, load_stock_signal_history
from quantamental.dashboard.ui import (
    COLOR,
    SIGNAL_LABELS,
    bucket_color,
    panel_header,
    padded_range,
    regime_color,
    sector_composite_color,
    style_plot,
)
from quantamental.portfolio.stoploss import check_stops
from quantamental.portfolio.tracker import compute_pnl


def _pct(value) -> str:
    try:
        return f"{float(value):.1%}"
    except (TypeError, ValueError):
        return "0.0%"


def _score(value) -> str:
    try:
        return f"{float(value):+.0f}"
    except (TypeError, ValueError):
        return "+0"


def _num(value, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _latest_context(signals_df: pd.DataFrame, sector_df: pd.DataFrame) -> dict:
    latest_signal = signals_df.iloc[-1] if not signals_df.empty else {}
    latest_sector = sector_df.iloc[-1] if not sector_df.empty else {}
    regime = latest_signal.get("confirmed_regime") or latest_signal.get("regime", "UNKNOWN")
    return {
        "regime": regime,
        "macro_score": _num(latest_signal.get("composite_score", 0)),
        "sector_score": _num(latest_sector.get("sector_composite", 0)),
    }


def _target_vs_current(
    alpha_ranks: pd.DataFrame,
    positions_df: pd.DataFrame,
    latest_prices: dict[str, float],
) -> pd.DataFrame:
    targets = pd.DataFrame(columns=["symbol", "target_weight", "bucket", "alpha_score", "rank"])
    if not alpha_ranks.empty:
        keep = ["symbol", "target_weight", "bucket", "alpha_score", "rank"]
        targets = alpha_ranks[[c for c in keep if c in alpha_ranks.columns]].copy()
    if "target_weight" not in targets:
        targets["target_weight"] = 0.0

    current = pd.DataFrame(columns=["symbol", "current_weight"])
    if not positions_df.empty:
        pnl = compute_pnl(positions_df, latest_prices)
        current = pnl[["symbol", "weight", "pnl_pct"]].copy()
        current["current_weight"] = pd.to_numeric(current["weight"], errors="coerce").fillna(0) / 100.0
        current = current.drop(columns=["weight"])

    symbols = sorted(set(targets.get("symbol", [])) | set(current.get("symbol", [])))
    if not symbols:
        return pd.DataFrame()

    frame = pd.DataFrame({"symbol": symbols})
    frame = frame.merge(targets, on="symbol", how="left")
    frame = frame.merge(current, on="symbol", how="left")
    frame["target_weight"] = pd.to_numeric(frame["target_weight"], errors="coerce").fillna(0.0)
    frame["current_weight"] = pd.to_numeric(frame["current_weight"], errors="coerce").fillna(0.0)
    frame["drift"] = frame["target_weight"] - frame["current_weight"]
    frame["bucket"] = frame["bucket"].fillna("NOT_RANKED")
    frame["alpha_score"] = pd.to_numeric(frame["alpha_score"], errors="coerce").fillna(0.0)
    frame["rank"] = pd.to_numeric(frame["rank"], errors="coerce")

    def action(row):
        if row["current_weight"] > 0 and row["target_weight"] == 0:
            return "EXIT/REVIEW"
        if row["current_weight"] == 0 and row["target_weight"] > 0:
            return "NEW BUY"
        if row["drift"] > 0.025:
            return "ADD"
        if row["drift"] < -0.025:
            return "TRIM"
        if row["target_weight"] > 0:
            return "HOLD"
        return "WATCH"

    frame["action"] = frame.apply(action, axis=1)
    return frame.sort_values(["target_weight", "alpha_score", "symbol"], ascending=[False, False, True])


def _risk_flags(context: dict, alpha_ranks: pd.DataFrame, positions_df: pd.DataFrame) -> list[tuple[str, str]]:
    flags: list[tuple[str, str]] = []
    exposure = float(alpha_ranks.get("target_weight", pd.Series(dtype=float)).sum()) if not alpha_ranks.empty else 0
    if context["regime"] == "RISK_OFF" or context["macro_score"] < -4:
        flags.append(("risk", "Macro risk-off: block new buys"))
    elif context["macro_score"] < 0:
        flags.append(("watch", "Macro below neutral"))
    else:
        flags.append(("ok", "Macro permits risk"))
    if context["sector_score"] < 0:
        flags.append(("watch", "Sector cap active"))
    else:
        flags.append(("ok", "Sector support intact"))
    if exposure < 0.5:
        flags.append(("watch", f"Target exposure only {exposure:.0%}"))
    if positions_df.empty:
        flags.append(("watch", "No open positions"))
    return flags


def _top_actions(
    context: dict,
    alpha_ranks: pd.DataFrame,
    positions_df: pd.DataFrame,
    latest_prices: dict[str, float],
) -> list[tuple[str, str, str]]:
    actions: list[tuple[str, str, str]] = []
    comparison = _target_vs_current(alpha_ranks, positions_df, latest_prices)
    if context["regime"] == "RISK_OFF" or context["macro_score"] < -4:
        actions.append(("risk", "De-risk", "Macro regime blocks new long exposure. Prioritize cash and stop review."))
    elif context["sector_score"] < 0:
        actions.append(("hold", "Selective only", "Sector score is negative. Keep exposure capped and require strong stock evidence."))
    else:
        actions.append(("deploy", "Deploy selectively", "Macro and sector context allow new positions from top-ranked names."))

    if not comparison.empty:
        new_buy = comparison[comparison["action"].eq("NEW BUY")].head(1)
        trim = comparison[comparison["action"].isin(["TRIM", "EXIT/REVIEW"])].head(1)
        add = comparison[comparison["action"].eq("ADD")].head(1)
        if not new_buy.empty:
            r = new_buy.iloc[0]
            actions.append(("deploy", f"Research {r['symbol']}", f"Top-ranked unowned name, target {r['target_weight']:.0%}."))
        if not add.empty:
            r = add.iloc[0]
            actions.append(("deploy", f"Add {r['symbol']}", f"Current weight is below target by {r['drift']:.1%}."))
        if not trim.empty:
            r = trim.iloc[0]
            actions.append(("risk", f"Review {r['symbol']}", f"Portfolio weight is above target or no longer ranked for allocation."))

    if len(actions) < 3 and not alpha_ranks.empty:
        top = alpha_ranks.head(1).iloc[0]
        actions.append(("hold", f"Monitor {top['symbol']}", f"Highest alpha score today: {float(top['alpha_score']):.1f}."))
    return actions[:3]


def render_overview(
    signals_df: pd.DataFrame,
    sector_df: pd.DataFrame,
    alpha_ranks: pd.DataFrame,
    positions_df: pd.DataFrame,
    latest_prices: dict[str, float],
):
    panel_header("Command Center", "Today", "decision queue")

    context = _latest_context(signals_df, sector_df)
    latest_alpha = alpha_ranks.iloc[0] if not alpha_ranks.empty else {}

    regime = context["regime"]
    macro_score = context["macro_score"]
    sector_score = context["sector_score"]
    top_symbol = latest_alpha.get("symbol", "—")
    top_score = _num(latest_alpha.get("alpha_score", 0))
    deployed = float(alpha_ranks.get("target_weight", pd.Series(dtype=float)).sum()) if not alpha_ranks.empty else 0.0
    stance = "Deploy" if macro_score >= 0 and sector_score >= 0 else "Hold"
    if regime == "RISK_OFF" or macro_score < -4:
        stance = "De-risk"

    cols = st.columns(5)
    cols[0].metric("Stance", stance, str(regime))
    cols[1].metric("Sector", _score(sector_score), "composite")
    cols[2].metric("Top Alpha", str(top_symbol), f"{top_score:.1f}")
    cols[3].metric("Target Risk", _pct(deployed))
    cols[4].metric("Open Positions", len(positions_df))

    actions = _top_actions(context, alpha_ranks, positions_df, latest_prices)
    action_html = "".join(
        f"<div class='q-action-card q-action-card-{kind}'><strong>{title}</strong><span>{body}</span></div>"
        for kind, title, body in actions
    )
    st.markdown(f"<div class='q-action-grid'>{action_html}</div>", unsafe_allow_html=True)

    flags = _risk_flags(context, alpha_ranks, positions_df)
    flag_html = "".join(
        f"<span class='q-flag q-flag-{kind}'>{text}</span>"
        for kind, text in flags
    )
    st.markdown(flag_html, unsafe_allow_html=True)

    left, right = st.columns([1.05, 1])
    with left:
        panel_header("Top Opportunities", "Alpha book", "highest conviction")
        _render_opportunity_cards(alpha_ranks, limit=6)
    with right:
        panel_header("Rebalance Queue", "Current vs target", "next review")
        comparison = _target_vs_current(alpha_ranks, positions_df, latest_prices)
        if comparison.empty:
            st.info("No rebalance candidates yet.")
        else:
            _render_blotter_cards(comparison, limit=6)


def render_panel_a(signals_df: pd.DataFrame, compact: bool = False):
    panel_header("Macro Regime", "Risk context", "confirmed signal")
    if signals_df.empty:
        st.info("No signal data available yet.")
        return

    latest = signals_df.iloc[-1]
    regime = latest.get("confirmed_regime") or latest.get("regime", "UNKNOWN")
    raw_regime = latest.get("regime", regime)
    composite = int(latest.get("composite_score", 0))

    color = regime_color(regime)
    st.markdown(
        f"""
        <div class="q-panel">
            <div class="q-kicker">Composite score</div>
            <div style="font-size:2rem; line-height:1; font-weight:700; color:{color};">
                {regime} <span style="font-size:1.25rem;">({composite:+d})</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if raw_regime != regime:
        st.caption(f"Unconfirmed raw regime: {raw_regime}")

    cols = st.columns(4)
    for col, (key, label) in zip(cols, SIGNAL_LABELS.items()):
        score = int(latest.get(key, 0))
        col.metric(label, f"{score:+d}", delta_color="normal")


def render_panel_b(positions_df: pd.DataFrame, latest_prices: dict[str, float], compact: bool = False):
    panel_header("Portfolio", "Positions", "live prices")
    if positions_df.empty:
        st.info("No open positions. Add positions via `portfolio/tracker.py`.")
        return

    df = compute_pnl(positions_df, latest_prices)
    display = df[["symbol", "entry_price", "current_price", "shares", "pnl", "pnl_pct", "weight"]].copy()
    display = display.rename(
        columns={
            "entry_price": "Entry $",
            "current_price": "Current $",
            "pnl": "P&L $",
            "pnl_pct": "P&L %",
            "weight": "Weight %",
        }
    )

    def color_pnl(val):
        if isinstance(val, float):
            c = COLOR["positive"] if val >= 0 else COLOR["negative"]
            return f"color: {c}"
        return ""

    styled = display.style.applymap(color_pnl, subset=["P&L $", "P&L %"])
    st.dataframe(styled, use_container_width=True, hide_index=True, height=240 if compact else None)

    total_pnl = df["pnl"].sum() if "pnl" in df else 0
    pnl_color = COLOR["positive"] if total_pnl >= 0 else COLOR["negative"]
    st.markdown(
        f"<div class='q-chip'>Total P&L <b style='color:{pnl_color}'>${total_pnl:,.2f}</b></div>",
        unsafe_allow_html=True,
    )


def render_panel_c(positions_df: pd.DataFrame, latest_prices: dict[str, float]):
    panel_header("Stop-Loss Monitor", "Risk", "alert zone")
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
        progress = min(max(distance_pct / 0.20, 0.0), 1.0)

        is_alert = symbol in alert_symbols
        color_label = "🔴" if is_alert else "🟢"
        label = (
            f"{color_label} {symbol}: ${current:.2f} | stop ${stop:.2f} | "
            f"{distance_pct*100:.1f}% above stop"
        )
        st.progress(float(progress), text=label)


def _action_class(action: str) -> str:
    if action == "NEW BUY":
        return "new"
    if action == "ADD":
        return "add"
    if action == "TRIM":
        return "trim"
    if action == "EXIT/REVIEW":
        return "exit"
    if action == "HOLD":
        return "hold"
    return "watch"


def _render_blotter_cards(comparison: pd.DataFrame, limit: int = 8):
    if comparison.empty:
        return
    priority = {"NEW BUY": 0, "ADD": 1, "TRIM": 2, "EXIT/REVIEW": 3, "HOLD": 4, "WATCH": 5}
    cards = comparison.copy()
    cards["priority"] = cards["action"].map(priority).fillna(9)
    cards = cards.sort_values(["priority", "rank", "symbol"], ascending=[True, True, True]).head(limit)
    html = []
    for _, row in cards.iterrows():
        action = str(row["action"])
        klass = _action_class(action)
        symbol = escape(str(row["symbol"]))
        action_text = escape(action)
        html.append(
            "<div class='q-blotter-card'>"
            "<div class='q-blotter-top'>"
            f"<span class='q-ticker'>{symbol}</span>"
            f"<span class='q-action-pill q-action-{klass}'>{action_text}</span>"
            "</div>"
            "<div class='q-mini-row'>"
            f"<span>Current<b>{row['current_weight']:.1%}</b></span>"
            f"<span>Target<b>{row['target_weight']:.1%}</b></span>"
            f"<span>Drift<b>{row['drift']:+.1%}</b></span>"
            "</div>"
            "</div>"
        )
    st.markdown(f"<div class='q-blotter-grid'>{''.join(html)}</div>", unsafe_allow_html=True)


def _render_opportunity_cards(alpha_ranks: pd.DataFrame, limit: int = 6):
    if alpha_ranks.empty:
        st.info("No alpha ranks saved yet.")
        return
    rows = alpha_ranks.head(limit)
    html = []
    for _, row in rows.iterrows():
        bucket = str(row.get("bucket", "WATCH"))
        action_class = "new" if bucket == "TOP_BUY" else "hold" if bucket == "HOLD" else "watch"
        if bucket == "BUY":
            action_class = "add"
        if bucket == "AVOID":
            action_class = "watch"
        symbol = escape(str(row.get("symbol", "—")))
        bucket_text = escape(bucket.replace("_", " "))
        html.append(
            "<div class='q-blotter-card'>"
            "<div class='q-blotter-top'>"
            f"<span class='q-ticker'>{symbol}</span>"
            f"<span class='q-action-pill q-action-{action_class}'>{bucket_text}</span>"
            "</div>"
            "<div class='q-mini-row'>"
            f"<span>Alpha<b>{_num(row.get('alpha_score')):.1f}</b></span>"
            f"<span>Target<b>{_num(row.get('target_weight')):.1%}</b></span>"
            f"<span>Rank<b>{int(_num(row.get('rank'), 0)) or '—'}</b></span>"
            "</div>"
            "</div>"
        )
    st.markdown(f"<div class='q-blotter-grid'>{''.join(html)}</div>", unsafe_allow_html=True)


def render_portfolio_risk(
    alpha_ranks: pd.DataFrame,
    positions_df: pd.DataFrame,
    latest_prices: dict[str, float],
    compact: bool = False,
):
    panel_header("Current vs Target", "Portfolio risk", "rebalance map")
    comparison = _target_vs_current(alpha_ranks, positions_df, latest_prices)
    if comparison.empty:
        st.info("No alpha targets or open positions to compare yet.")
        return

    _render_blotter_cards(comparison, limit=6 if compact else 10)
    if compact:
        return

    display = comparison[
        [
            "symbol",
            "action",
            "current_weight",
            "target_weight",
            "drift",
            "bucket",
            "alpha_score",
        ]
    ].copy()
    display = display.rename(
        columns={
            "symbol": "Ticker",
            "action": "Action",
            "current_weight": "Current",
            "target_weight": "Target",
            "drift": "Drift",
            "bucket": "Bucket",
            "alpha_score": "Alpha",
        }
    )
    for col in ["Current", "Target", "Drift"]:
        display[col] = display[col].map(lambda v: f"{float(v):+.1%}" if col == "Drift" else f"{float(v):.1%}")
    display["Alpha"] = display["Alpha"].map(lambda v: f"{float(v):.1f}")
    if compact:
        display = display[display["Action"].isin(["NEW BUY", "ADD", "TRIM", "EXIT/REVIEW"])].head(8)
        if display.empty:
            display = comparison.head(6).copy()
            display = display.rename(
                columns={
                    "symbol": "Ticker",
                    "action": "Action",
                    "current_weight": "Current",
                    "target_weight": "Target",
                    "drift": "Drift",
                    "bucket": "Bucket",
                    "alpha_score": "Alpha",
                }
            )
            for col in ["Current", "Target", "Drift"]:
                display[col] = display[col].map(lambda v: f"{float(v):+.1%}" if col == "Drift" else f"{float(v):.1%}")
            display["Alpha"] = display["Alpha"].map(lambda v: f"{float(v):.1f}")
            display = display[["Ticker", "Action", "Current", "Target", "Drift", "Bucket", "Alpha"]]
    st.dataframe(display, use_container_width=True, hide_index=True, height=300 if compact else None)


def _add_regime_zones(fig: go.Figure):
    zone_colors = {
        "RISK_ON": "rgba(39,174,96,0.12)",
        "MODERATE_ON": "rgba(39,174,96,0.06)",
        "NEUTRAL": "rgba(243,156,18,0.10)",
        "MODERATE_OFF": "rgba(231,76,60,0.06)",
        "RISK_OFF": "rgba(231,76,60,0.12)",
    }
    for regime, y0, y1 in [
        ("RISK_ON", 5, 8),
        ("MODERATE_ON", 2, 4),
        ("NEUTRAL", -1, 1),
        ("MODERATE_OFF", -4, -2),
        ("RISK_OFF", -8, -5),
    ]:
        fig.add_hrect(
            y0=y0,
            y1=y1,
            fillcolor=zone_colors[regime],
            line_width=0,
            annotation_text=regime,
            annotation_position="right",
            annotation=dict(font_size=10, font_color=COLOR.get(regime, "#888")),
        )


def render_panel_d(signals_df: pd.DataFrame):
    panel_header("Signal History", "Macro", "60 days")
    if signals_df.empty or len(signals_df) < 2:
        st.info("Not enough signal history yet.")
        return

    fig = go.Figure()
    _add_regime_zones(fig)
    fig.add_trace(
        go.Scatter(
            x=signals_df["ts"],
            y=signals_df["composite_score"],
            mode="lines+markers",
            name="Composite Score",
            line=dict(color=COLOR["info"], width=2),
            marker=dict(size=5),
        )
    )
    fig.update_layout(
        yaxis=dict(title="Composite Score", range=[-8, 8], zeroline=True),
        xaxis=dict(title="Date"),
    )
    style_plot(fig, height=340)
    st.plotly_chart(fig, use_container_width=True)


def render_panel_f(sector_df: pd.DataFrame):
    panel_header("Sector Signals", "AI-infra", "SMH / SPY")
    if sector_df.empty:
        st.info("No sector signals yet. Populate by running:\n\n`python scripts/daily_pipeline.py --step calc_sector_signals`")
        return

    latest = sector_df.iloc[-1]
    composite = int(latest.get("sector_composite", 0))
    color = sector_composite_color(composite)
    st.markdown(
        f"<h3 style='color:{color}; text-align:center'>Sector Composite: {composite:+d}</h3>",
        unsafe_allow_html=True,
    )

    tiles = [
        ("sox_spx_signal", "A — SOX/SPX"),
        ("tsmc_signal", "B — TSMC Revenue"),
        ("capex_signal", "C — Hyperscaler CapEx"),
        ("api_pricing_signal", "D — AI API Pricing"),
    ]
    cols = st.columns(4)
    for col, (key, label) in zip(cols, tiles):
        score = int(latest.get(key, 0) or 0)
        col.metric(label, f"{score:+d}")

    if {"sox_spx_ratio", "sox_spx_ema20", "sox_spx_ema60"}.issubset(sector_df.columns):
        panel_header("SMH / SPY Relative Strength", "Signal A", "trend")
        ratio_fig = go.Figure()
        ratio_fig.add_trace(go.Scatter(x=sector_df["ts"], y=sector_df["sox_spx_ratio"], name="SMH/SPY", mode="lines", line=dict(color=COLOR["info"], width=2)))
        ratio_fig.add_trace(go.Scatter(x=sector_df["ts"], y=sector_df["sox_spx_ema20"], name="EMA(20)", mode="lines", line=dict(color=COLOR["positive"], width=1.5, dash="dash")))
        ratio_fig.add_trace(go.Scatter(x=sector_df["ts"], y=sector_df["sox_spx_ema60"], name="EMA(60)", mode="lines", line=dict(color=COLOR["negative"], width=1.5, dash="dot")))
        y_range = padded_range(
            pd.concat(
                [
                    sector_df["sox_spx_ratio"],
                    sector_df["sox_spx_ema20"],
                    sector_df["sox_spx_ema60"],
                ],
                ignore_index=True,
            )
        )
        ratio_fig.update_layout(
            yaxis=dict(title="Ratio", range=y_range),
            xaxis=dict(title=None),
        )
        style_plot(ratio_fig, height=380, showlegend=True)
        st.plotly_chart(ratio_fig, use_container_width=True)

    if "sector_composite" in sector_df.columns and len(sector_df) >= 2:
        panel_header("Sector Composite History", "Signal layer", "90 days")
        comp_fig = go.Figure()
        _add_regime_zones(comp_fig)
        comp_fig.add_trace(go.Scatter(x=sector_df["ts"], y=sector_df["sector_composite"], mode="lines+markers", name="Sector Composite", line=dict(color=COLOR["info"], width=2), marker=dict(size=5)))
        comp_fig.update_layout(
            yaxis=dict(title="Composite", range=[-8, 8], zeroline=True),
            xaxis=dict(title=None),
        )
        style_plot(comp_fig, height=300)
        st.plotly_chart(comp_fig, use_container_width=True)


def render_panel_g():
    panel_header("Stock Technicals", "Single-name", "EMA / RSI / volume")

    grouped = load_candidate_list_by_sector()
    sectors = [s for s, tickers in grouped.items() if tickers]
    if not sectors:
        st.info("No candidates configured. Add some via Panel E.")
        return

    col_sec, col_sym = st.columns([1, 1])
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

    tiles = [
        ("ema_signal", "EMA(20/60)"),
        ("rsi_signal", "RSI(14)"),
        ("volume_signal", "Volume"),
        ("pead_signal", "PEAD"),
    ]
    cols = st.columns(4)
    for col, (key, label) in zip(cols, tiles):
        score = int(latest.get(key, 0) or 0)
        col.metric(label, f"{score:+d}")

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.5, 0.25, 0.25],
        vertical_spacing=0.04,
        subplot_titles=("Price & EMA(20/60)", "RSI(14)", "Volume vs 20-day MA"),
    )

    if not ohlcv_df.empty:
        fig.add_trace(go.Scatter(x=ohlcv_df["ts"], y=ohlcv_df["close"], name="Close", mode="lines", line=dict(color=COLOR["info"], width=1.5)), row=1, col=1)
    if "ema_20" in sig_df.columns:
        fig.add_trace(go.Scatter(x=sig_df["ts"], y=sig_df["ema_20"], name="EMA(20)", mode="lines", line=dict(color=COLOR["positive"], width=1.2, dash="dash")), row=1, col=1)
    if "ema_60" in sig_df.columns:
        fig.add_trace(go.Scatter(x=sig_df["ts"], y=sig_df["ema_60"], name="EMA(60)", mode="lines", line=dict(color=COLOR["negative"], width=1.2, dash="dot")), row=1, col=1)

    if "rsi_14" in sig_df.columns:
        fig.add_trace(go.Scatter(x=sig_df["ts"], y=sig_df["rsi_14"], name="RSI(14)", mode="lines", line=dict(color=COLOR["info"], width=1.5), showlegend=False), row=2, col=1)
        fig.add_hline(y=70, line=dict(color=COLOR["negative"], width=1, dash="dash"), row=2, col=1)
        fig.add_hline(y=30, line=dict(color=COLOR["positive"], width=1, dash="dash"), row=2, col=1)
        fig.update_yaxes(range=[0, 100], row=2, col=1)

    if not ohlcv_df.empty and "volume" in ohlcv_df.columns:
        fig.add_trace(go.Bar(x=ohlcv_df["ts"], y=ohlcv_df["volume"], name="Volume", marker=dict(color=COLOR["inactive"]), showlegend=False), row=3, col=1)
        vol_ma = ohlcv_df["volume"].rolling(20).mean()
        fig.add_trace(go.Scatter(x=ohlcv_df["ts"], y=vol_ma, name="Vol MA(20)", mode="lines", line=dict(color=COLOR["info"], width=1.5), showlegend=False), row=3, col=1)

    style_plot(fig, height=620, showlegend=True)
    st.plotly_chart(fig, use_container_width=True)

    comp_color = sector_composite_color(composite)
    st.markdown(
        f"<div style='text-align:center'><b>Stock composite for {symbol}: "
        f"<span style='color:{comp_color}'>{composite:+d}</span></b> "
        f"&nbsp;(range −7 … +7)</div>",
        unsafe_allow_html=True,
    )


def render_panel_h_alpha(ranks: pd.DataFrame | None = None, compact: bool = False):
    panel_header("Alpha Book", "Selection", "V1 long-only")
    if ranks is None:
        ranks = load_latest_alpha_ranks()
    if ranks.empty:
        st.info(
            "No alpha ranks saved yet. Run:\n\n"
            "`python scripts/run_alpha.py --asof YYYY-MM-DD`"
        )
        return

    latest_asof = ranks.get("asof_date", pd.Series(["unknown"])).iloc[0]
    deployed = float(ranks.get("target_weight", pd.Series(dtype=float)).sum())
    buys_allowed = bool(ranks.get("new_buys_allowed", pd.Series([False])).iloc[0])

    c1, c2, c3 = st.columns(3)
    c1.metric("As of", str(latest_asof))
    c2.metric("Target exposure", f"{deployed:.0%}")
    c3.metric("New buys", "Allowed" if buys_allowed else "Blocked")

    if not compact and "bucket" in ranks:
        counts = ranks["bucket"].value_counts()
        bcols = st.columns(4)
        for col, bucket in zip(bcols, ["TOP_BUY", "BUY", "HOLD", "AVOID"]):
            col.metric(bucket.replace("_", " "), int(counts.get(bucket, 0)))

    display_cols = [
        "rank",
        "symbol",
        "bucket",
        "alpha_score",
        "target_weight",
        "stock_composite",
        "momentum_20",
        "volatility_20",
        "drawdown_60",
        "macro_regime",
        "sector_score",
    ]
    existing = [c for c in display_cols if c in ranks.columns]
    display = ranks[existing].copy()
    if "target_weight" in display:
        display["target_weight"] = display["target_weight"].map(lambda v: f"{float(v):.1%}")
    rename = {
        "rank": "Rank",
        "symbol": "Ticker",
        "bucket": "Bucket",
        "alpha_score": "Alpha",
        "target_weight": "Target",
        "stock_composite": "Stock",
        "momentum_20": "Momentum",
        "volatility_20": "Vol",
        "drawdown_60": "Drawdown",
        "macro_regime": "Macro",
        "sector_score": "Sector",
    }
    display = display.rename(columns=rename)
    if compact:
        display = display.head(8)

    def bucket_style(value):
        color = bucket_color(str(value).replace(" ", "_"))
        return f"color: {color}; font-weight: 700"

    styled = display.style.map(bucket_style, subset=["Bucket"]) if "Bucket" in display else display
    st.dataframe(styled, use_container_width=True, hide_index=True, height=330 if compact else None)


def render_panel_e_candidate_editor():
    panel_header("Candidate List", "Universe", "sector aware")

    grouped = load_candidate_list_by_sector()
    flat_total = sum(len(t) for t in grouped.values())
    source = candidate_list_source()
    meta = candidate_list_metadata()

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

        ordered_sectors: list[str] = list(BASE_CANDIDATES.keys())
        for s in grouped.keys():
            if s not in ordered_sectors and s != UNCATEGORIZED_SECTOR:
                ordered_sectors.append(s)
        if UNCATEGORIZED_SECTOR in grouped:
            ordered_sectors.append(UNCATEGORIZED_SECTOR)

        edits: dict[str, list[str]] = {}
        for sector in ordered_sectors:
            current = grouped.get(sector, [])
            edits[sector] = st.multiselect(
                f"**{sector}** ({len(current)})",
                options=pool,
                default=current,
                key=f"candidate_editor_{sector}",
                help=f"Tickers in '{sector}'. Picking a ticker that's currently in another sector will move it on save.",
            )

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
            resolved: dict[str, list[str]] = {}
            seen: dict[str, str] = {}
            for sector, tickers in reversed(list(edits.items())):
                kept = []
                for t in tickers:
                    if t in seen:
                        continue
                    seen[t] = sector
                    kept.append(t)
                if kept:
                    resolved[sector] = sorted(kept)
            ordered_resolved = {s: resolved[s] for s in edits.keys() if s in resolved}

            if not any(ordered_resolved.values()):
                st.warning("At least one ticker is required — refusing to save empty list.")
            else:
                path = save_candidate_list(ordered_resolved, note=note)
                total = sum(len(t) for t in ordered_resolved.values())
                st.toast(f"Saved {total} candidates across {len(ordered_resolved)} sectors → {path.name}", icon="✅")
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
