"""Review-first earnings event importer for PEAD."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Callable

import pandas as pd

from quantamental.config.settings import FMP_API_KEY, SQLITE_PATH
from quantamental.signals.earnings import (
    load_earnings_events,
    log_earnings_event,
    winsorize_surprise_pct,
)


EarningsFetcher = Callable[[str, date, date], pd.DataFrame]


class ProviderAccessError(RuntimeError):
    """Provider rejected the request because the endpoint or symbol is plan-gated."""


def default_import_window(asof: date | None = None, days: int = 45) -> tuple[date, date]:
    end = asof or date.today()
    return end - timedelta(days=days), end


def _date_arg(value: str | date | pd.Timestamp) -> date:
    return pd.Timestamp(value).date()


def _first_value(row: pd.Series, names: tuple[str, ...]):
    for name in names:
        if name in row and pd.notna(row[name]):
            return row[name]
    return None


def _safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _surprise_percent_points(value) -> float | None:
    parsed = _safe_float(value)
    if parsed is None:
        return None
    # yfinance commonly returns 0.125 for +12.5% despite the column being named
    # Surprise(%). Manual logs and PEAD storage use percent points.
    return parsed * 100.0 if abs(parsed) <= 1.0 else parsed


def _derive_surprise_percent_points(eps_actual: float | None, eps_estimate: float | None) -> float | None:
    if eps_actual is None or eps_estimate is None or eps_estimate == 0:
        return None
    return (eps_actual - eps_estimate) / abs(eps_estimate) * 100.0


def _append_note(notes: str | None, addition: str | None) -> str | None:
    if not addition:
        return notes
    if not notes:
        return addition
    if addition in notes:
        return notes
    return f"{notes}; {addition}"


def _winsorized_surprise_and_notes(
    surprise_pct: float | None,
    notes: str | None,
) -> tuple[float | None, str | None]:
    if surprise_pct is None:
        return None, notes
    capped, winsor_note = winsorize_surprise_pct(surprise_pct)
    return capped, _append_note(notes, winsor_note)


def _redact_api_key_from_url(text: str) -> str:
    if "apikey=" not in text:
        return text
    words = text.split()
    redacted = []
    for word in words:
        if "apikey=" not in word:
            redacted.append(word)
            continue
        trailing = ""
        if word[-1:] in ".,;)":
            trailing = word[-1]
            word = word[:-1]
        try:
            parts = urlsplit(word)
            query = urlencode(
                [(key, "REDACTED" if key.lower() == "apikey" else value) for key, value in parse_qsl(parts.query)]
            )
            redacted.append(urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment)) + trailing)
        except ValueError:
            redacted.append("URL_WITH_REDACTED_API_KEY" + trailing)
    return " ".join(redacted)


def _provider_error_status(exc: Exception) -> tuple[str, str]:
    message = _redact_api_key_from_url(str(exc))
    lowered = message.lower()
    if "402" in lowered or "payment required" in lowered or "plan" in lowered:
        return "PLAN_LIMIT", message
    if "429" in lowered or "too many requests" in lowered or "rate limit" in lowered:
        return "RATE_LIMIT", message
    return "FETCH_ERROR", message


def parse_yfinance_earnings_frame(
    symbol: str,
    frame: pd.DataFrame,
    start: date | str | pd.Timestamp,
    end: date | str | pd.Timestamp,
) -> pd.DataFrame:
    """Convert yfinance earnings dates output into PEAD event rows."""
    columns = [
        "symbol",
        "report_date",
        "fiscal_period",
        "eps_actual",
        "eps_estimate",
        "surprise_pct",
        "source",
        "notes",
        "status",
        "reason",
    ]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)

    start_date = _date_arg(start)
    end_date = _date_arg(end)
    working = frame.copy()
    if "report_date" in working:
        raw_dates = working["report_date"]
    elif "Earnings Date" in working:
        raw_dates = working["Earnings Date"]
    elif "Date" in working:
        raw_dates = working["Date"]
    else:
        raw_dates = working.index

    parsed_dates = pd.to_datetime(raw_dates, errors="coerce", utc=True)
    if isinstance(parsed_dates, pd.Series):
        working["report_date"] = parsed_dates.dt.date
    else:
        working["report_date"] = pd.Series(parsed_dates, index=working.index).dt.date
    working = working[working["report_date"].notna()].copy()
    working = working[(working["report_date"] >= start_date) & (working["report_date"] <= end_date)]

    rows: list[dict] = []
    for _, row in working.iterrows():
        eps_actual = _safe_float(
            _first_value(row, ("Reported EPS", "reported_eps", "eps_actual", "Actual EPS"))
        )
        eps_estimate = _safe_float(
            _first_value(row, ("EPS Estimate", "eps_estimate", "Estimated EPS", "Estimate"))
        )
        surprise_pct = _surprise_percent_points(
            _first_value(row, ("Surprise(%)", "Surprise %", "surprise_pct", "eps_surprise_pct"))
        )
        if surprise_pct is None:
            surprise_pct = _derive_surprise_percent_points(eps_actual, eps_estimate)
        notes = "automated_import:yfinance"
        surprise_pct, notes = _winsorized_surprise_and_notes(surprise_pct, notes)

        status = "READY" if surprise_pct is not None else "SKIPPED"
        reason = "" if surprise_pct is not None else "missing surprise and EPS actual/estimate"
        rows.append(
            {
                "symbol": symbol.strip().upper(),
                "report_date": row["report_date"].isoformat(),
                "fiscal_period": _first_value(row, ("fiscal_period", "Fiscal Period", "period")),
                "eps_actual": eps_actual,
                "eps_estimate": eps_estimate,
                "surprise_pct": surprise_pct,
                "source": "yfinance",
                "notes": notes,
                "status": status,
                "reason": reason,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def parse_earnings_csv_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize a user-provided earnings CSV into PEAD event rows.

    Required columns:
      - symbol
      - report_date
      - surprise_pct, or both eps_actual and eps_estimate
    """
    columns = [
        "symbol",
        "report_date",
        "fiscal_period",
        "eps_actual",
        "eps_estimate",
        "surprise_pct",
        "source",
        "notes",
        "status",
        "reason",
    ]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)

    working = frame.copy()
    working.columns = [str(col).strip().lower() for col in working.columns]
    rows: list[dict] = []

    for idx, row in working.iterrows():
        symbol = str(row.get("symbol", "")).strip().upper()
        parsed_date = pd.to_datetime(row.get("report_date"), errors="coerce")
        eps_actual = _safe_float(row.get("eps_actual"))
        eps_estimate = _safe_float(row.get("eps_estimate"))
        surprise_pct = _surprise_percent_points(row.get("surprise_pct"))
        if surprise_pct is None:
            surprise_pct = _derive_surprise_percent_points(eps_actual, eps_estimate)
        source = row.get("source") if pd.notna(row.get("source")) else "csv"
        notes = row.get("notes") if pd.notna(row.get("notes")) else "manual_csv"
        surprise_pct, notes = _winsorized_surprise_and_notes(surprise_pct, str(notes))

        reasons = []
        if not symbol:
            reasons.append("missing symbol")
        if pd.isna(parsed_date):
            reasons.append("missing/invalid report_date")
        if surprise_pct is None:
            reasons.append("missing surprise and EPS actual/estimate")

        status = "READY" if not reasons else "SKIPPED"
        report_date = None if pd.isna(parsed_date) else pd.Timestamp(parsed_date).date().isoformat()

        rows.append(
            {
                "symbol": symbol,
                "report_date": report_date,
                "fiscal_period": row.get("fiscal_period") if pd.notna(row.get("fiscal_period")) else None,
                "eps_actual": eps_actual,
                "eps_estimate": eps_estimate,
                "surprise_pct": surprise_pct,
                "source": str(source),
                "notes": notes,
                "status": status,
                "reason": "; ".join(reasons) if reasons else "",
            }
        )
    return pd.DataFrame(rows, columns=columns)


def parse_fmp_earnings_frame(
    symbol: str,
    frame: pd.DataFrame,
    start: date | str | pd.Timestamp,
    end: date | str | pd.Timestamp,
) -> pd.DataFrame:
    """Convert Financial Modeling Prep earnings rows into PEAD event rows."""
    columns = [
        "symbol",
        "report_date",
        "fiscal_period",
        "eps_actual",
        "eps_estimate",
        "surprise_pct",
        "source",
        "notes",
        "status",
        "reason",
    ]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)

    start_date = _date_arg(start)
    end_date = _date_arg(end)
    working = frame.copy()
    working.columns = [str(col).strip() for col in working.columns]

    rows: list[dict] = []
    for _, row in working.iterrows():
        raw_symbol = _first_value(row, ("symbol", "Symbol", "ticker", "Ticker")) or symbol
        parsed_date = pd.to_datetime(
            _first_value(row, ("date", "report_date", "fiscalDateEnding", "acceptedDate")),
            errors="coerce",
        )
        if pd.isna(parsed_date):
            continue
        report_date = pd.Timestamp(parsed_date).date()
        if report_date < start_date or report_date > end_date:
            continue

        eps_actual = _safe_float(
            _first_value(row, ("epsActual", "eps_actual", "actualEps", "reportedEPS", "eps"))
        )
        eps_estimate = _safe_float(
            _first_value(row, ("epsEstimated", "eps_estimate", "estimatedEPS", "epsEstimate"))
        )
        surprise_pct = _surprise_percent_points(
            _first_value(
                row,
                (
                    "surprisePct",
                    "surprisePercentage",
                    "surprise_percent",
                    "surprise_pct",
                    "epsSurprisePercent",
                ),
            )
        )
        if surprise_pct is None:
            surprise_pct = _derive_surprise_percent_points(eps_actual, eps_estimate)
        notes = "automated_import:fmp"
        surprise_pct, notes = _winsorized_surprise_and_notes(surprise_pct, notes)

        fiscal_period = _first_value(
            row,
            ("period", "fiscal_period", "fiscalDateEnding", "quarter", "time"),
        )
        status = "READY" if surprise_pct is not None else "SKIPPED"
        reason = "" if surprise_pct is not None else "missing surprise and EPS actual/estimate"
        rows.append(
            {
                "symbol": str(raw_symbol).strip().upper(),
                "report_date": report_date.isoformat(),
                "fiscal_period": fiscal_period,
                "eps_actual": eps_actual,
                "eps_estimate": eps_estimate,
                "surprise_pct": surprise_pct,
                "source": "fmp",
                "notes": notes,
                "status": status,
                "reason": reason,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def import_earnings_events_from_csv(
    csv_path: str | Path,
    *,
    commit: bool = False,
    overwrite: bool = False,
    path: str = SQLITE_PATH,
) -> pd.DataFrame:
    frame = pd.read_csv(csv_path)
    parsed = parse_earnings_csv_frame(frame)
    return write_imported_earnings_events(parsed, commit=commit, overwrite=overwrite, path=path)


def fetch_yfinance_earnings_events(symbol: str, start: date, end: date) -> pd.DataFrame:
    """Fetch and parse recent earnings dates from yfinance for one ticker."""
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    if hasattr(ticker, "get_earnings_dates"):
        raw = ticker.get_earnings_dates(limit=24)
    else:
        raw = getattr(ticker, "earnings_dates", pd.DataFrame())
    return parse_yfinance_earnings_frame(symbol, raw, start, end)


def fetch_fmp_earnings_events(
    symbol: str,
    start: date,
    end: date,
    *,
    api_key: str = FMP_API_KEY,
) -> pd.DataFrame:
    """Fetch and parse earnings events from Financial Modeling Prep."""
    if not api_key:
        raise ValueError("missing FMP_API_KEY")

    import requests

    url = "https://financialmodelingprep.com/stable/earnings"
    params = {"symbol": symbol.strip().upper(), "apikey": api_key}
    response = requests.get(url, params=params, timeout=20)
    status_code = getattr(response, "status_code", None)
    if status_code == 402:
        raise ProviderAccessError(
            f"FMP endpoint or symbol requires a paid plan: {symbol.strip().upper()}"
        )
    if status_code == 429:
        raise RuntimeError(f"FMP rate limit response for {symbol.strip().upper()}")
    response.raise_for_status()
    payload = response.json()

    if isinstance(payload, dict) and "Error Message" in payload:
        raise RuntimeError(str(payload["Error Message"]))
    if isinstance(payload, dict) and "message" in payload:
        message = str(payload["message"])
        if "limit" in message.lower() or "plan" in message.lower():
            raise ProviderAccessError(f"FMP limit/plan response: {message}")
        raise RuntimeError(message)
    if not isinstance(payload, list):
        raise RuntimeError(f"unexpected FMP response shape: {type(payload).__name__}")

    raw = pd.DataFrame(payload)
    return parse_fmp_earnings_frame(symbol, raw, start, end)


def write_imported_earnings_events(
    events: pd.DataFrame,
    *,
    commit: bool = False,
    overwrite: bool = False,
    path: str = SQLITE_PATH,
) -> pd.DataFrame:
    if events is None or events.empty:
        return events.copy() if events is not None else pd.DataFrame()

    out = events.copy()
    if "event_id" not in out:
        out["event_id"] = None
    ready = out["status"].eq("READY")
    if commit and ready.any():
        for idx, row in out[ready].iterrows():
            existing = load_earnings_events(
                symbols=[str(row["symbol"])],
                start=str(row["report_date"]),
                end=str(row["report_date"]),
                path=path,
            )
            if not existing.empty and not overwrite:
                out.at[idx, "status"] = "EXISTS"
                out.at[idx, "reason"] = "existing event; re-run with --overwrite to update"
                continue
            event_id = log_earnings_event(
                symbol=str(row["symbol"]),
                report_date=str(row["report_date"]),
                fiscal_period=row.get("fiscal_period"),
                surprise_pct=float(row["surprise_pct"]),
                eps_actual=_safe_float(row.get("eps_actual")),
                eps_estimate=_safe_float(row.get("eps_estimate")),
                source=str(row.get("source") or "import"),
                notes=str(row.get("notes") or "imported"),
                path=path,
            )
            out.at[idx, "event_id"] = event_id
            out.at[idx, "status"] = "WRITTEN"
    elif not commit:
        out.loc[ready, "status"] = "DRY_RUN"
    return out


def _fetcher_source(fetcher: EarningsFetcher) -> str:
    name = getattr(fetcher, "__name__", "")
    if "fmp" in name:
        return "fmp"
    if "yfinance" in name:
        return "yfinance"
    return "import"


def import_earnings_events(
    symbols: list[str],
    start: date | str | pd.Timestamp,
    end: date | str | pd.Timestamp,
    *,
    commit: bool = False,
    overwrite: bool = False,
    path: str = SQLITE_PATH,
    fetcher: EarningsFetcher = fetch_yfinance_earnings_events,
) -> pd.DataFrame:
    """Fetch earnings events and optionally write them to SQLite.

    Dry-run is the default. Set ``commit=True`` to upsert events into
    ``earnings_events``.
    """
    start_date = _date_arg(start)
    end_date = _date_arg(end)
    clean_symbols = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
    results: list[pd.DataFrame] = []
    provider = _fetcher_source(fetcher)

    for symbol in clean_symbols:
        try:
            fetched = fetcher(symbol, start_date, end_date)
        except Exception as exc:  # pragma: no cover - network/provider defensive path
            status, reason = _provider_error_status(exc)
            results.append(
                pd.DataFrame(
                    [
                        {
                            "symbol": symbol,
                            "report_date": None,
                            "fiscal_period": None,
                            "eps_actual": None,
                            "eps_estimate": None,
                            "surprise_pct": None,
                            "source": provider,
                            "notes": None,
                            "status": status,
                            "reason": reason,
                            "event_id": None,
                        }
                    ]
                )
            )
            continue

        if fetched is None or fetched.empty:
            results.append(
                pd.DataFrame(
                    [
                        {
                            "symbol": symbol,
                            "report_date": None,
                            "fiscal_period": None,
                            "eps_actual": None,
                            "eps_estimate": None,
                            "surprise_pct": None,
                            "source": provider,
                            "notes": None,
                            "status": "NO_EVENTS",
                            "reason": f"no events in {start_date}..{end_date}",
                            "event_id": None,
                        }
                    ]
                )
            )
            continue

        results.append(
            write_imported_earnings_events(
                fetched,
                commit=commit,
                overwrite=overwrite,
                path=path,
            )
        )

    if not results:
        return pd.DataFrame()
    combined = pd.concat(results, ignore_index=True)
    return combined.sort_values(["symbol", "report_date"], na_position="last").reset_index(drop=True)
