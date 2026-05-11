"""Transparent cross-sectional alpha ranking."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd


COMPONENT_WEIGHTS = {
    "stock_composite": 0.30,
    "ema_signal": 0.15,
    # Standalone RSI was harmful in multi-window diagnostics. It remains inside
    # stock_composite, but V1 no longer double-counts it as a separate factor.
    "rsi_signal": 0.00,
    "volume_signal": 0.08,
    # PEAD remains collected and displayed, but is observe-only until provider
    # consensus quality is validated across sources.
    "pead_signal": 0.00,
    "momentum_20": 0.15,
    # Diagnostics showed the low-volatility reward was harmful in the current
    # validation window. Keep the feature available, but do not score it in V1.
    "volatility_20": 0.00,
    "drawdown_60": 0.05,
}

MAX_PRICE_AGE_DAYS = 7


def _percentile_component(series: pd.Series, *, higher_is_better: bool = True) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    if values.notna().sum() <= 1 or values.nunique(dropna=True) <= 1:
        return pd.Series(0.0, index=series.index)
    ranks = values.rank(method="average", pct=True)
    if not higher_is_better:
        ranks = 1.0 - ranks
    return (ranks.fillna(0.5) * 2.0 - 1.0).clip(-1.0, 1.0)


def _bounded_context(value: float, max_abs: float) -> float:
    if max_abs == 0:
        return 0.0
    try:
        return float(np.clip(value / max_abs, -1.0, 1.0))
    except (TypeError, ValueError):
        return 0.0


def _bucket(rank: int, total: int, alpha_score: float) -> str:
    if total <= 0:
        return "AVOID"
    pct = rank / total
    if pct <= 0.20 and alpha_score >= 60:
        return "TOP_BUY"
    if pct <= 0.50 and alpha_score >= 50:
        return "BUY"
    if alpha_score >= 40:
        return "HOLD"
    return "AVOID"


def rank_alpha(features: pd.DataFrame) -> pd.DataFrame:
    """Rank feature rows into explainable alpha buckets."""
    if features.empty:
        return features.copy()

    df = features.copy()
    component_cols = []
    for col, weight in COMPONENT_WEIGHTS.items():
        if col not in df:
            df[col] = 0.0
        higher = col != "volatility_20"
        component = _percentile_component(df[col], higher_is_better=higher)
        component_col = f"{col}_component"
        df[component_col] = (component * weight).round(6)
        component_cols.append(component_col)

    macro_score = df["macro_score"] if "macro_score" in df else pd.Series(0.0, index=df.index)
    sector_score = df["sector_score"] if "sector_score" in df else pd.Series(0.0, index=df.index)
    df["context_macro_component"] = macro_score.apply(lambda v: _bounded_context(v, 8.0) * 0.10)
    df["context_sector_component"] = sector_score.apply(lambda v: _bounded_context(v, 8.0) * 0.10)
    price_age = pd.to_numeric(
        df.get("price_age_days", pd.Series(0, index=df.index)),
        errors="coerce",
    ).fillna(9999)
    close = pd.to_numeric(df.get("close", pd.Series(0, index=df.index)), errors="coerce").fillna(0)
    no_price = close <= 0
    stale_price = price_age > MAX_PRICE_AGE_DAYS
    df["data_quality_flag"] = "OK"
    df.loc[stale_price, "data_quality_flag"] = "STALE_PRICE"
    df.loc[no_price, "data_quality_flag"] = "NO_PRICE"
    df["data_quality_component"] = 0.0
    df.loc[stale_price, "data_quality_component"] = -0.75
    df.loc[no_price, "data_quality_component"] = -1.00

    raw_cols = component_cols + [
        "context_macro_component",
        "context_sector_component",
        "data_quality_component",
    ]
    df["alpha_raw"] = df[raw_cols].sum(axis=1).clip(-1.0, 1.0)
    df["alpha_score"] = (50.0 + 50.0 * df["alpha_raw"]).round(2)
    df = df.sort_values(["alpha_score", "symbol"], ascending=[False, True]).reset_index(drop=True)
    df["rank"] = np.arange(1, len(df) + 1)
    total = len(df)
    df["bucket"] = [
        _bucket(int(rank), total, float(score))
        for rank, score in zip(df["rank"], df["alpha_score"], strict=False)
    ]
    df.loc[df["data_quality_flag"].ne("OK"), "bucket"] = "AVOID"

    def components_json(row: pd.Series) -> str:
        payload = {col: round(float(row[col]), 4) for col in raw_cols}
        return json.dumps(payload, sort_keys=True)

    df["score_components"] = df.apply(components_json, axis=1)
    return df
