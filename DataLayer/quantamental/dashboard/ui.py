import plotly.graph_objects as go
import streamlit as st


COLOR = {
    "RISK_ON": "#138A5B",
    "MODERATE_ON": "#2DA66F",
    "NEUTRAL": "#B7791F",
    "MODERATE_OFF": "#C05621",
    "RISK_OFF": "#C53030",
    "positive": "#138A5B",
    "negative": "#C53030",
    "warning": "#B7791F",
    "info": "#2563A6",
    "inactive": "#64748B",
    "ink": "#172033",
    "muted": "#667085",
    "line": "#D9E2EC",
    "surface": "#FFFFFF",
    "background": "#F5F7FA",
    "accent": "#6A4C93",
}

SIGNAL_LABELS = {
    "yield_10y_signal": "10Y Yield",
    "vix_signal": "VIX",
    "fed_bs_signal": "Fed Balance Sheet",
    "credit_spread_signal": "Credit Spread (IG OAS)",
}


def regime_color(regime: str) -> str:
    return COLOR.get(regime, COLOR["inactive"])


def sector_composite_color(score: int) -> str:
    if score >= 5:
        return COLOR["RISK_ON"]
    if score >= 2:
        return COLOR["MODERATE_ON"]
    if score >= -1:
        return COLOR["NEUTRAL"]
    if score >= -4:
        return COLOR["MODERATE_OFF"]
    return COLOR["RISK_OFF"]


def bucket_color(bucket: str) -> str:
    return {
        "TOP_BUY": COLOR["positive"],
        "BUY": COLOR["info"],
        "HOLD": COLOR["warning"],
        "AVOID": COLOR["inactive"],
    }.get(bucket, COLOR["inactive"])


def apply_global_styles():
    st.markdown(
        f"""
        <style>
        :root {{
            --q-bg: {COLOR["background"]};
            --q-surface: {COLOR["surface"]};
            --q-ink: {COLOR["ink"]};
            --q-muted: {COLOR["muted"]};
            --q-line: {COLOR["line"]};
        }}
        .stApp {{
            background: var(--q-bg);
            color: var(--q-ink);
        }}
        .block-container {{
            padding-top: 1.35rem;
            padding-bottom: 2rem;
            max-width: 1240px;
        }}
        h1, h2, h3 {{
            color: var(--q-ink);
            letter-spacing: 0;
        }}
        h1 {{
            font-size: 2rem;
            line-height: 1.15;
            margin-bottom: 0.2rem;
        }}
        h2, h3 {{
            margin-top: 0.35rem;
        }}
        div[data-testid="stMetric"] {{
            background: var(--q-surface);
            border: 1px solid var(--q-line);
            border-radius: 8px;
            padding: 0.78rem 0.9rem;
            min-height: 92px;
        }}
        div[data-testid="stMetric"] label {{
            color: var(--q-muted);
            font-size: 0.78rem;
        }}
        div[data-testid="stMetricValue"] {{
            font-size: 1.35rem;
            color: var(--q-ink);
        }}
        .q-panel {{
            background: var(--q-surface);
            border: 1px solid var(--q-line);
            border-radius: 8px;
            padding: 1rem;
            margin-bottom: 0.9rem;
        }}
        .q-kicker {{
            color: var(--q-muted);
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin-bottom: 0.25rem;
        }}
        .q-title-row {{
            display: flex;
            justify-content: space-between;
            gap: 0.75rem;
            align-items: baseline;
            margin-bottom: 0.7rem;
        }}
        .q-title-row h2 {{
            margin: 0;
            font-size: 1.15rem;
        }}
        .q-chip {{
            display: inline-block;
            padding: 0.18rem 0.48rem;
            border: 1px solid var(--q-line);
            border-radius: 999px;
            color: var(--q-muted);
            font-size: 0.78rem;
            background: #FAFBFC;
            white-space: nowrap;
        }}
        .q-action-grid {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 0.75rem;
            margin: 0.5rem 0 1rem 0;
        }}
        .q-action-card {{
            background: var(--q-surface);
            border: 1px solid var(--q-line);
            border-radius: 8px;
            padding: 0.85rem 0.95rem;
            min-height: 92px;
        }}
        .q-action-card-deploy {{
            border-left: 4px solid {COLOR["positive"]};
        }}
        .q-action-card-hold {{
            border-left: 4px solid {COLOR["warning"]};
        }}
        .q-action-card-risk {{
            border-left: 4px solid {COLOR["negative"]};
        }}
        .q-action-card strong {{
            display: block;
            font-size: 0.92rem;
            margin-bottom: 0.28rem;
        }}
        .q-action-card span {{
            color: var(--q-muted);
            font-size: 0.82rem;
            line-height: 1.35;
        }}
        .q-flag {{
            display: inline-block;
            margin: 0.12rem 0.22rem 0.12rem 0;
            padding: 0.28rem 0.55rem;
            border-radius: 999px;
            font-size: 0.78rem;
            border: 1px solid var(--q-line);
            background: #FAFBFC;
            color: var(--q-muted);
        }}
        .q-flag-risk {{
            border-color: #F2C9C9;
            background: #FFF6F6;
            color: {COLOR["negative"]};
        }}
        .q-flag-ok {{
            border-color: #BFE5D1;
            background: #F3FBF6;
            color: {COLOR["positive"]};
        }}
        .q-flag-watch {{
            border-color: #F3D6A5;
            background: #FFF9EC;
            color: {COLOR["warning"]};
        }}
        .q-blotter-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 0.65rem;
            margin: 0.45rem 0 0.85rem 0;
        }}
        .q-blotter-card {{
            background: var(--q-surface);
            border: 1px solid var(--q-line);
            border-radius: 8px;
            padding: 0.78rem 0.85rem;
        }}
        .q-blotter-top {{
            display: flex;
            justify-content: space-between;
            gap: 0.6rem;
            align-items: baseline;
            margin-bottom: 0.45rem;
        }}
        .q-ticker {{
            font-weight: 700;
            color: var(--q-ink);
            font-size: 1rem;
        }}
        .q-action-pill {{
            font-size: 0.72rem;
            border-radius: 999px;
            padding: 0.16rem 0.46rem;
            border: 1px solid var(--q-line);
            color: var(--q-muted);
            white-space: nowrap;
        }}
        .q-action-new, .q-action-add {{
            color: {COLOR["positive"]};
            background: #F3FBF6;
            border-color: #BFE5D1;
        }}
        .q-action-trim, .q-action-exit {{
            color: {COLOR["negative"]};
            background: #FFF6F6;
            border-color: #F2C9C9;
        }}
        .q-action-hold {{
            color: {COLOR["info"]};
            background: #F2F7FC;
            border-color: #C7DDF3;
        }}
        .q-action-watch {{
            color: {COLOR["warning"]};
            background: #FFF9EC;
            border-color: #F3D6A5;
        }}
        .q-mini-row {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 0.35rem;
            color: var(--q-muted);
            font-size: 0.76rem;
        }}
        .q-mini-row b {{
            display: block;
            color: var(--q-ink);
            font-size: 0.88rem;
            margin-top: 0.08rem;
        }}
        .stTabs [data-baseweb="tab-list"] {{
            gap: 0.35rem;
            overflow-x: auto;
            padding-bottom: 0.25rem;
        }}
        .stTabs [data-baseweb="tab"] {{
            background: var(--q-surface);
            border: 1px solid var(--q-line);
            border-radius: 8px;
            padding: 0.45rem 0.75rem;
            color: var(--q-muted) !important;
            opacity: 1 !important;
            white-space: nowrap;
        }}
        .stTabs [data-baseweb="tab"] * {{
            color: var(--q-muted) !important;
            opacity: 1 !important;
            visibility: visible !important;
        }}
        .stTabs [data-baseweb="tab"]:hover,
        .stTabs [data-baseweb="tab"]:hover * {{
            color: var(--q-ink) !important;
        }}
        .stTabs [aria-selected="true"] {{
            border-color: {COLOR["info"]};
            color: {COLOR["info"]} !important;
        }}
        .stTabs [aria-selected="true"],
        .stTabs [aria-selected="true"] * {{
            color: {COLOR["info"]} !important;
            opacity: 1 !important;
            visibility: visible !important;
        }}
        div[data-testid="stButton"] button {{
            color: var(--q-ink) !important;
            opacity: 1 !important;
        }}
        div[data-testid="stButton"] button * {{
            color: inherit !important;
            opacity: 1 !important;
            visibility: visible !important;
        }}
        div[data-testid="stButton"] button:hover,
        div[data-testid="stButton"] button:hover * {{
            color: {COLOR["info"]} !important;
        }}
        div[data-testid="stDataFrame"] {{
            border: 1px solid var(--q-line);
            border-radius: 8px;
        }}
        @media (max-width: 720px) {{
            .block-container {{
                padding-left: 0.75rem;
                padding-right: 0.75rem;
            }}
            h1 {{
                font-size: 1.45rem;
            }}
            div[data-testid="stMetric"] {{
                min-height: 76px;
                padding: 0.62rem 0.72rem;
            }}
            div[data-testid="stMetricValue"] {{
                font-size: 1.08rem;
            }}
            .q-panel {{
                padding: 0.75rem;
            }}
            .q-action-grid {{
                grid-template-columns: 1fr;
            }}
            .q-blotter-grid {{
                grid-template-columns: 1fr;
            }}
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def panel_header(title: str, kicker: str | None = None, chip: str | None = None):
    kicker_html = f"<div class='q-kicker'>{kicker}</div>" if kicker else ""
    chip_html = f"<span class='q-chip'>{chip}</span>" if chip else ""
    st.markdown(
        f"""
        <div class="q-title-row">
            <div>{kicker_html}<h2>{title}</h2></div>
            <div>{chip_html}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def style_plot(fig: go.Figure, *, height: int = 340, showlegend: bool = False) -> go.Figure:
    fig.update_layout(
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=COLOR["ink"], size=12),
        margin=dict(l=34, r=22, t=18, b=34),
        hovermode="x unified",
        showlegend=showlegend,
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.16,
            xanchor="left",
            x=0,
            bgcolor="rgba(255,255,255,0)",
            font=dict(size=11, color=COLOR["muted"]),
        ),
    )
    fig.update_xaxes(
        showgrid=False,
        zeroline=False,
        linecolor=COLOR["line"],
        tickfont=dict(color=COLOR["muted"]),
    )
    fig.update_yaxes(
        gridcolor=COLOR["line"],
        zerolinecolor=COLOR["line"],
        linecolor=COLOR["line"],
        tickfont=dict(color=COLOR["muted"]),
    )
    return fig


def padded_range(values, *, pad_ratio: float = 0.12):
    clean = [float(v) for v in values if v is not None]
    clean = [v for v in clean if v == v]
    if not clean:
        return None
    lo = min(clean)
    hi = max(clean)
    if lo == hi:
        pad = abs(lo) * 0.02 or 1.0
    else:
        pad = (hi - lo) * pad_ratio
    return [lo - pad, hi + pad]
