"""Point-in-time alpha feature construction.

The feature builder is deliberately DataFrame-first so it can be tested without
QuestDB and reused by live ranking, reporting, and backtests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd


DEFAULT_BENCHMARK = "SMH"
DEFAULT_MARKET_BENCHMARKS = ("SMH", "SPY", "QQQ")
STOCK_SIGNAL_COLUMNS = (
    "ema_20",
    "ema_60",
    "ema_signal",
    "rsi_14",
    "rsi_signal",
    "volume_ratio",
    "volume_signal",
    "pead_signal",
    "stock_composite",
)


@dataclass(frozen=True)
class FeatureInputs:
    ohlcv: pd.DataFrame
    stock_signals: pd.DataFrame
    regime_signals: pd.DataFrame
    sector_signals: pd.DataFrame
    earnings_events: pd.DataFrame | None = None


def _as_timestamp(value: date | str | pd.Timestamp | None) -> pd.Timestamp:
    if value is None:
        return pd.Timestamp.utcnow().normalize()
    return pd.Timestamp(value)


def _normalize_ts(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "ts" not in df.columns:
        return df.copy()
    out = df.copy()
    out["ts"] = pd.to_datetime(out["ts"])
    if getattr(out["ts"].dt, "tz", None) is not None:
        out["ts"] = out["ts"].dt.tz_convert(None)
    return out


def _latest_on_or_before(df: pd.DataFrame, asof: pd.Timestamp) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    working = _normalize_ts(df)
    return working[working["ts"] <= asof].sort_values("ts")


def _latest_by_symbol(df: pd.DataFrame, asof: pd.Timestamp) -> pd.DataFrame:
    working = _latest_on_or_before(df, asof)
    if working.empty or "symbol" not in working.columns:
        return pd.DataFrame()
    return working.groupby("symbol", as_index=False).tail(1)


def _latest_row(df: pd.DataFrame, asof: pd.Timestamp) -> pd.Series | None:
    working = _latest_on_or_before(df, asof)
    if working.empty:
        return None
    return working.iloc[-1]


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _symbol_risk_rows(ohlcv: pd.DataFrame, symbols: list[str], asof: pd.Timestamp) -> pd.DataFrame:
    working = _latest_on_or_before(ohlcv, asof)
    if working.empty:
        return pd.DataFrame({"symbol": symbols})

    records = []
    for symbol in symbols:
        group = working[working["symbol"] == symbol].sort_values("ts").tail(90)
        if group.empty:
            records.append({"symbol": symbol})
            continue

        close = pd.to_numeric(group["close"], errors="coerce")
        volume = pd.to_numeric(group.get("volume", pd.Series(index=group.index)), errors="coerce")
        returns = close.pct_change()
        latest_close = _safe_float(close.iloc[-1], np.nan)
        high_60 = close.tail(60).max()
        drawdown_60 = (latest_close / high_60 - 1.0) if high_60 and not pd.isna(high_60) else 0.0

        records.append(
            {
                "symbol": symbol,
                "asof_date": asof.date().isoformat(),
                "close": latest_close,
                "momentum_20": _safe_float(close.pct_change(20).iloc[-1]),
                "volatility_20": _safe_float(returns.tail(20).std() * np.sqrt(252)),
                "addv_20": _safe_float((close * volume).tail(20).mean()),
                "drawdown_60": _safe_float(drawdown_60),
            }
        )
    return pd.DataFrame(records)


def _beta_rows(
    ohlcv: pd.DataFrame,
    symbols: list[str],
    asof: pd.Timestamp,
    benchmark: str,
) -> pd.DataFrame:
    working = _latest_on_or_before(ohlcv, asof)
    if working.empty:
        return pd.DataFrame({"symbol": symbols, "beta_60": 1.0})

    pivot = (
        working.pivot_table(index="ts", columns="symbol", values="close", aggfunc="last")
        .sort_index()
        .tail(90)
    )
    if benchmark not in pivot:
        return pd.DataFrame({"symbol": symbols, "beta_60": 1.0})

    returns = pivot.pct_change(fill_method=None).tail(60)
    bench = returns[benchmark]
    bench_var = bench.var()
    records = []
    for symbol in symbols:
        if symbol not in returns or symbol == benchmark or not bench_var:
            beta = 1.0
        else:
            beta = returns[symbol].cov(bench) / bench_var
        records.append({"symbol": symbol, "beta_60": _safe_float(beta, 1.0)})
    return pd.DataFrame(records)


def _technical_stock_signal_rows(
    ohlcv: pd.DataFrame,
    symbols: list[str],
    asof: pd.Timestamp,
    earnings_events: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Compute OHLCV-only stock signals when historical stock_signals are absent."""
    working = _latest_on_or_before(ohlcv, asof)
    if working.empty:
        return pd.DataFrame({"symbol": symbols})

    from quantamental.signals.stock import score_one_ticker

    earnings_map = _earnings_event_map(earnings_events, symbols, asof)

    records = []
    for symbol in symbols:
        group = working[working["symbol"] == symbol].sort_values("ts").tail(120)
        if group.empty:
            records.append({"symbol": symbol})
            continue
        signal_group = group.copy()
        if "volume" not in signal_group:
            signal_group["volume"] = 0
        earnings_event = earnings_map.get(symbol)
        scored = score_one_ticker(
            signal_group[["ts", "close", "volume"]],
            earnings_event=earnings_event,
            asof=asof.date(),
        )
        record = {"symbol": symbol}
        for col in STOCK_SIGNAL_COLUMNS:
            if col in scored:
                record[col] = scored[col]
        record["pead_event_active"] = bool(earnings_event)
        records.append(record)
    return pd.DataFrame(records)


def _surprise_to_decimal(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    parsed = float(value)
    return parsed / 100.0 if abs(parsed) > 1 else parsed


def _earnings_event_map(
    earnings_events: pd.DataFrame | None,
    symbols: list[str],
    asof: pd.Timestamp,
) -> dict[str, dict]:
    if earnings_events is None or earnings_events.empty:
        return {}

    from quantamental.signals.stock import PEAD_DURATION_DAYS

    events = earnings_events.copy()
    if "symbol" not in events or "report_date" not in events:
        return {}
    events["symbol"] = events["symbol"].astype(str).str.upper()
    events["report_date"] = pd.to_datetime(events["report_date"], errors="coerce")
    if getattr(events["report_date"].dt, "tz", None) is not None:
        events["report_date"] = events["report_date"].dt.tz_convert(None)

    symbol_set = {str(s).strip().upper() for s in symbols if str(s).strip()}
    cutoff = asof - pd.Timedelta(days=PEAD_DURATION_DAYS)
    events = events[
        events["symbol"].isin(symbol_set)
        & events["report_date"].notna()
        & (events["report_date"] <= asof)
        & (events["report_date"] >= cutoff)
    ].sort_values("report_date")
    if events.empty:
        return {}

    surprise_col = "surprise_pct" if "surprise_pct" in events else "eps_surprise_pct"
    result = {}
    for _, row in events.groupby("symbol", as_index=False).tail(1).iterrows():
        result[str(row["symbol"])] = {
            "report_date": pd.Timestamp(row["report_date"]).date(),
            "eps_surprise_pct": _surprise_to_decimal(row.get(surprise_col)),
        }
    return result


def build_features(
    ohlcv: pd.DataFrame,
    stock_signals: pd.DataFrame | None = None,
    regime_signals: pd.DataFrame | None = None,
    sector_signals: pd.DataFrame | None = None,
    earnings_events: pd.DataFrame | None = None,
    symbols: list[str] | None = None,
    asof: date | str | pd.Timestamp | None = None,
    benchmark: str = DEFAULT_BENCHMARK,
) -> pd.DataFrame:
    """Build one point-in-time feature row per symbol.

    All inputs are filtered to ``ts <= asof`` before any feature is selected.
    """
    asof_ts = _as_timestamp(asof)
    ohlcv = _normalize_ts(ohlcv)
    stock_signals = _normalize_ts(stock_signals if stock_signals is not None else pd.DataFrame())
    regime_signals = _normalize_ts(regime_signals if regime_signals is not None else pd.DataFrame())
    sector_signals = _normalize_ts(sector_signals if sector_signals is not None else pd.DataFrame())
    earnings_events = earnings_events if earnings_events is not None else pd.DataFrame()

    if symbols is None:
        if not stock_signals.empty and "symbol" in stock_signals:
            symbols = sorted(stock_signals["symbol"].dropna().astype(str).unique())
        elif not ohlcv.empty and "symbol" in ohlcv:
            symbols = sorted(
                set(ohlcv["symbol"].dropna().astype(str).unique()) - set(DEFAULT_MARKET_BENCHMARKS)
            )
        else:
            symbols = []
    symbols = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})

    risk = _symbol_risk_rows(ohlcv, symbols, asof_ts)
    beta = _beta_rows(ohlcv, symbols, asof_ts, benchmark)
    latest_stock = _latest_by_symbol(stock_signals, asof_ts)

    rows = pd.DataFrame({"symbol": symbols})
    rows["asof_date"] = asof_ts.date().isoformat()
    rows = rows.merge(risk, on=["symbol"], how="left", suffixes=("", "_risk"))
    if "asof_date_risk" in rows:
        rows["asof_date"] = rows["asof_date_risk"].fillna(rows["asof_date"])
        rows = rows.drop(columns=["asof_date_risk"])
    rows = rows.merge(beta, on="symbol", how="left")
    if not latest_stock.empty:
        stock_payload = latest_stock.drop(
            columns=["ts", "open", "high", "low", "close", "volume"],
            errors="ignore",
        )
        rows = rows.merge(stock_payload, on="symbol", how="left")

    technical_fallback = _technical_stock_signal_rows(ohlcv, symbols, asof_ts, earnings_events)
    if not technical_fallback.empty:
        rows = rows.merge(technical_fallback, on="symbol", how="left", suffixes=("", "_fallback"))
        active_pead = (
            rows["pead_event_active"].fillna(False).astype(bool)
            if "pead_event_active" in rows
            else pd.Series(False, index=rows.index)
        )
        for col in STOCK_SIGNAL_COLUMNS:
            fallback_col = f"{col}_fallback"
            if fallback_col not in rows:
                continue
            if col not in rows or rows[col].isna().all():
                rows[col] = rows[fallback_col]
            else:
                missing = rows[col].isna()
                rows.loc[missing, col] = rows.loc[missing, fallback_col]
                if col in {"pead_signal", "stock_composite"}:
                    rows.loc[active_pead, col] = rows.loc[active_pead, fallback_col]
            rows = rows.drop(columns=[fallback_col])
        rows = rows.drop(columns=["pead_event_active"], errors="ignore")

    regime = _latest_row(regime_signals, asof_ts)
    sector = _latest_row(sector_signals, asof_ts)
    rows["macro_score"] = _safe_float(regime.get("composite_score") if regime is not None else 0)
    if regime is not None:
        rows["macro_regime"] = regime.get("confirmed_regime") or regime.get("regime") or "UNKNOWN"
    else:
        rows["macro_regime"] = "UNKNOWN"
    rows["sector_score"] = _safe_float(sector.get("sector_composite") if sector is not None else 0)
    rows["smh_spy_signal"] = _safe_float(sector.get("sox_spx_signal") if sector is not None else 0)

    defaults = {
        "close": 0.0,
        "momentum_20": 0.0,
        "volatility_20": 0.0,
        "addv_20": 0.0,
        "drawdown_60": 0.0,
        "beta_60": 1.0,
        "ema_signal": 0,
        "rsi_signal": 0,
        "volume_signal": 0,
        "pead_signal": 0,
        "stock_composite": 0,
        "rsi_14": 50.0,
        "volume_ratio": 0.0,
    }
    for col, default in defaults.items():
        if col not in rows:
            rows[col] = default
        rows[col] = pd.to_numeric(rows[col], errors="coerce").fillna(default)

    from quantamental.signals.stock import stock_composite_score

    rows["stock_composite"] = rows.apply(
        lambda row: stock_composite_score(
            int(row["ema_signal"]),
            int(row["rsi_signal"]),
            int(row["volume_signal"]),
            int(row["pead_signal"]),
        ),
        axis=1,
    )

    return rows.sort_values("symbol").reset_index(drop=True)


def add_forward_returns(
    features: pd.DataFrame,
    ohlcv: pd.DataFrame,
    horizons: tuple[int, ...] = (20, 40),
    benchmark: str = DEFAULT_BENCHMARK,
) -> pd.DataFrame:
    """Attach next-close forward returns and excess returns.

    A feature row dated ``t`` enters on the first close strictly after ``t``.
    The exit is ``horizon`` trading rows after entry. This avoids same-day
    lookahead.
    """
    if features.empty:
        return features.copy()

    prices = _normalize_ts(ohlcv)
    pivot = prices.pivot_table(index="ts", columns="symbol", values="close", aggfunc="last").sort_index()
    out = features.copy()

    for horizon in horizons:
        out[f"fwd_{horizon}d_return"] = np.nan
        out[f"fwd_{horizon}d_excess_{benchmark}"] = np.nan

    for idx, row in out.iterrows():
        symbol = row["symbol"]
        asof_ts = pd.Timestamp(row["asof_date"])
        future_dates = pivot.index[pivot.index > asof_ts]
        if symbol not in pivot or benchmark not in pivot or len(future_dates) == 0:
            continue
        entry_date = future_dates[0]
        entry_symbol = pivot.at[entry_date, symbol]
        entry_bench = pivot.at[entry_date, benchmark]
        if pd.isna(entry_symbol) or pd.isna(entry_bench) or entry_symbol == 0 or entry_bench == 0:
            continue
        entry_pos = pivot.index.get_loc(entry_date)
        for horizon in horizons:
            exit_pos = entry_pos + horizon
            if exit_pos >= len(pivot.index):
                continue
            exit_date = pivot.index[exit_pos]
            exit_symbol = pivot.at[exit_date, symbol]
            exit_bench = pivot.at[exit_date, benchmark]
            if pd.isna(exit_symbol) or pd.isna(exit_bench):
                continue
            symbol_ret = exit_symbol / entry_symbol - 1.0
            bench_ret = exit_bench / entry_bench - 1.0
            out.at[idx, f"fwd_{horizon}d_return"] = symbol_ret
            out.at[idx, f"fwd_{horizon}d_excess_{benchmark}"] = symbol_ret - bench_ret
    return out


def load_feature_inputs_from_questdb(
    symbols: list[str],
    asof: date | str | pd.Timestamp | None = None,
    lookback_days: int = 260,
) -> FeatureInputs:
    """Load the inputs needed for live ranking from QuestDB."""
    from quantamental.data.ingest.questdb_writer import query, symbol_list_clause

    asof_ts = _as_timestamp(asof)
    start_ts = asof_ts - pd.Timedelta(days=lookback_days)
    universe = sorted(set(symbols) | set(DEFAULT_MARKET_BENCHMARKS))
    clause, params = symbol_list_clause(universe)
    params["asof"] = asof_ts.isoformat()
    params["start"] = start_ts.isoformat()

    ohlcv = query(
        f"""
        SELECT symbol, ts, open, high, low, close, volume
        FROM daily_ohlcv
        WHERE symbol IN ({clause})
          AND ts <= :asof
          AND ts >= :start
        ORDER BY symbol, ts
        """,
        params,
    )
    stock = query(
        f"""
        SELECT *
        FROM stock_signals
        WHERE symbol IN ({clause}) AND ts <= :asof
        ORDER BY symbol, ts
        """,
        params,
    )
    regime = query(
        "SELECT * FROM regime_signals WHERE ts <= :asof ORDER BY ts",
        {"asof": asof_ts.isoformat()},
    )
    sector = query(
        "SELECT * FROM sector_signals WHERE ts <= :asof ORDER BY ts",
        {"asof": asof_ts.isoformat()},
    )
    from quantamental.signals.earnings import load_earnings_events

    earnings = load_earnings_events(symbols=symbols, start=start_ts, end=asof_ts)
    return FeatureInputs(
        ohlcv=ohlcv,
        stock_signals=stock,
        regime_signals=regime,
        sector_signals=sector,
        earnings_events=earnings,
    )


def load_backtest_inputs_from_questdb(
    symbols: list[str],
    start: date | str,
    end: date | str,
    warmup_days: int = 260,
    forward_days: int = 60,
) -> FeatureInputs:
    """Load a backtest window with warmup and forward-label buffer."""
    from quantamental.data.ingest.questdb_writer import query, symbol_list_clause

    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    query_start = start_ts - pd.Timedelta(days=warmup_days)
    query_end = end_ts + pd.Timedelta(days=forward_days)
    universe = sorted(set(symbols) | set(DEFAULT_MARKET_BENCHMARKS))
    clause, params = symbol_list_clause(universe)
    params.update({"start": query_start.isoformat(), "end": query_end.isoformat()})

    ohlcv = query(
        f"""
        SELECT symbol, ts, open, high, low, close, volume
        FROM daily_ohlcv
        WHERE symbol IN ({clause}) AND ts >= :start AND ts <= :end
        ORDER BY symbol, ts
        """,
        params,
    )
    stock = query(
        f"""
        SELECT *
        FROM stock_signals
        WHERE symbol IN ({clause}) AND ts >= :start AND ts <= :end
        ORDER BY symbol, ts
        """,
        params,
    )
    regime = query(
        "SELECT * FROM regime_signals WHERE ts >= :start AND ts <= :end ORDER BY ts",
        {"start": query_start.isoformat(), "end": query_end.isoformat()},
    )
    sector = query(
        "SELECT * FROM sector_signals WHERE ts >= :start AND ts <= :end ORDER BY ts",
        {"start": query_start.isoformat(), "end": query_end.isoformat()},
    )
    from quantamental.signals.earnings import load_earnings_events

    earnings = load_earnings_events(symbols=symbols, start=query_start, end=query_end)
    return FeatureInputs(
        ohlcv=ohlcv,
        stock_signals=stock,
        regime_signals=regime,
        sector_signals=sector,
        earnings_events=earnings,
    )
