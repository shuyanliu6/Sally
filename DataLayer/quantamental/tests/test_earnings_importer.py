from __future__ import annotations

from datetime import date

import pandas as pd

from quantamental.signals.earnings import load_earnings_events, log_earnings_event
from quantamental.signals.earnings_importer import (
    ProviderAccessError,
    fetch_fmp_earnings_events,
    import_earnings_events_from_csv,
    import_earnings_events,
    parse_earnings_csv_frame,
    parse_fmp_earnings_frame,
    parse_yfinance_earnings_frame,
)


def test_parse_yfinance_earnings_frame_normalizes_surprise_ratio():
    raw = pd.DataFrame(
        {
            "EPS Estimate": [1.00],
            "Reported EPS": [1.125],
            "Surprise(%)": [0.125],
        },
        index=[pd.Timestamp("2026-05-01 08:00:00-04:00")],
    )

    parsed = parse_yfinance_earnings_frame("nvda", raw, "2026-04-01", "2026-05-31")

    assert len(parsed) == 1
    row = parsed.iloc[0]
    assert row["symbol"] == "NVDA"
    assert row["report_date"] == "2026-05-01"
    assert round(float(row["surprise_pct"]), 1) == 12.5
    assert row["status"] == "READY"


def test_parse_yfinance_earnings_frame_derives_surprise_when_missing():
    raw = pd.DataFrame(
        {
            "Earnings Date": ["2026-05-01"],
            "EPS Estimate": [1.00],
            "Reported EPS": [1.20],
        }
    )

    parsed = parse_yfinance_earnings_frame("amd", raw, "2026-04-01", "2026-05-31")

    assert round(float(parsed.iloc[0]["surprise_pct"]), 1) == 20.0
    assert parsed.iloc[0]["status"] == "READY"


def test_import_earnings_events_dry_run_writes_nothing(tmp_path):
    db_path = tmp_path / "meta.db"

    def fetcher(symbol: str, start: date, end: date) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "report_date": "2026-05-01",
                    "fiscal_period": "2026-Q1",
                    "eps_actual": 1.20,
                    "eps_estimate": 1.00,
                    "surprise_pct": 20.0,
                    "source": "test",
                    "notes": "mock",
                    "status": "READY",
                    "reason": "",
                }
            ]
        )

    report = import_earnings_events(
        ["NVDA"],
        "2026-04-01",
        "2026-05-31",
        path=str(db_path),
        fetcher=fetcher,
    )

    assert report.iloc[0]["status"] == "DRY_RUN"
    assert load_earnings_events(path=str(db_path)).empty


def test_import_earnings_events_commit_is_idempotent(tmp_path):
    db_path = tmp_path / "meta.db"

    def fetcher(symbol: str, start: date, end: date) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "report_date": "2026-05-01",
                    "fiscal_period": "2026-Q1",
                    "eps_actual": 1.20,
                    "eps_estimate": 1.00,
                    "surprise_pct": 20.0,
                    "source": "test",
                    "notes": "mock",
                    "status": "READY",
                    "reason": "",
                }
            ]
        )

    first = import_earnings_events(
        ["NVDA"],
        "2026-04-01",
        "2026-05-31",
        commit=True,
        path=str(db_path),
        fetcher=fetcher,
    )
    second = import_earnings_events(
        ["NVDA"],
        "2026-04-01",
        "2026-05-31",
        commit=True,
        path=str(db_path),
        fetcher=fetcher,
    )
    stored = load_earnings_events(symbols=["NVDA"], path=str(db_path))

    assert first.iloc[0]["status"] == "WRITTEN"
    assert second.iloc[0]["status"] == "EXISTS"
    assert len(stored) == 1
    assert round(float(stored.iloc[0]["surprise_pct"]), 1) == 20.0


def test_import_earnings_events_overwrite_updates_existing_event(tmp_path):
    db_path = tmp_path / "meta.db"
    surprise = 20.0

    def fetcher(symbol: str, start: date, end: date) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "report_date": "2026-05-01",
                    "fiscal_period": "2026-Q1",
                    "eps_actual": None,
                    "eps_estimate": None,
                    "surprise_pct": surprise,
                    "source": "test",
                    "notes": "mock",
                    "status": "READY",
                    "reason": "",
                }
            ]
        )

    import_earnings_events(
        ["NVDA"],
        "2026-04-01",
        "2026-05-31",
        commit=True,
        path=str(db_path),
        fetcher=fetcher,
    )
    surprise = -10.0
    report = import_earnings_events(
        ["NVDA"],
        "2026-04-01",
        "2026-05-31",
        commit=True,
        overwrite=True,
        path=str(db_path),
        fetcher=fetcher,
    )
    stored = load_earnings_events(symbols=["NVDA"], path=str(db_path))

    assert report.iloc[0]["status"] == "WRITTEN"
    assert len(stored) == 1
    assert round(float(stored.iloc[0]["surprise_pct"]), 1) == -10.0


def test_import_earnings_events_skips_incomplete_events(tmp_path):
    db_path = tmp_path / "meta.db"

    def fetcher(symbol: str, start: date, end: date) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "report_date": "2026-05-01",
                    "fiscal_period": None,
                    "eps_actual": None,
                    "eps_estimate": None,
                    "surprise_pct": None,
                    "source": "test",
                    "notes": "mock",
                    "status": "SKIPPED",
                    "reason": "missing surprise and EPS actual/estimate",
                }
            ]
        )

    report = import_earnings_events(
        ["NVDA"],
        "2026-04-01",
        "2026-05-31",
        commit=True,
        path=str(db_path),
        fetcher=fetcher,
    )

    assert report.iloc[0]["status"] == "SKIPPED"
    assert load_earnings_events(path=str(db_path)).empty


def test_parse_earnings_csv_frame_accepts_surprise_or_derives_it():
    raw = pd.DataFrame(
        [
            {
                "symbol": "nvda",
                "report_date": "2026-05-01",
                "fiscal_period": "2026-Q1",
                "surprise_pct": 12.5,
                "source": "manual_csv",
                "notes": "reviewed",
            },
            {
                "symbol": "amd",
                "report_date": "2026-05-02",
                "eps_actual": 1.20,
                "eps_estimate": 1.00,
            },
            {
                "symbol": "",
                "report_date": "bad-date",
            },
        ]
    )

    parsed = parse_earnings_csv_frame(raw)
    by_symbol = parsed.set_index("symbol")

    assert by_symbol.at["NVDA", "status"] == "READY"
    assert round(float(by_symbol.at["NVDA", "surprise_pct"]), 1) == 12.5
    assert by_symbol.at["AMD", "status"] == "READY"
    assert round(float(by_symbol.at["AMD", "surprise_pct"]), 1) == 20.0
    skipped = parsed[parsed["status"].eq("SKIPPED")].iloc[0]
    assert "missing symbol" in skipped["reason"]
    assert "missing/invalid report_date" in skipped["reason"]


def test_parse_fmp_earnings_frame_accepts_fmp_payload():
    raw = pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "date": "2026-05-01",
                "epsActual": 1.25,
                "epsEstimated": 1.00,
                "fiscalDateEnding": "2026-04-30",
            },
            {
                "symbol": "AMD",
                "date": "2026-05-02",
                "epsActual": 0.90,
                "epsEstimated": 1.00,
                "surprisePct": -10.0,
            },
        ]
    )

    parsed = parse_fmp_earnings_frame("NVDA", raw, "2026-04-01", "2026-05-31")
    by_symbol = parsed.set_index("symbol")

    assert by_symbol.at["NVDA", "status"] == "READY"
    assert round(float(by_symbol.at["NVDA", "surprise_pct"]), 1) == 25.0
    assert by_symbol.at["AMD", "status"] == "READY"
    assert round(float(by_symbol.at["AMD", "surprise_pct"]), 1) == -10.0
    assert by_symbol.at["NVDA", "source"] == "fmp"


def test_parse_fmp_earnings_frame_winsorizes_extreme_surprise():
    raw = pd.DataFrame(
        [
            {
                "symbol": "INTC",
                "date": "2026-04-23",
                "epsActual": 0.29,
                "epsEstimated": 0.01897,
            }
        ]
    )

    parsed = parse_fmp_earnings_frame("INTC", raw, "2026-04-01", "2026-05-31")
    row = parsed.iloc[0]

    assert round(float(row["surprise_pct"]), 1) == 100.0
    assert "raw_surprise_pct=1428.73" in row["notes"]
    assert "winsorized_to=100" in row["notes"]


def test_log_earnings_event_winsorizes_all_write_paths(tmp_path):
    db_path = tmp_path / "meta.db"

    log_earnings_event(
        "INTC",
        "2026-04-23",
        surprise_pct=1428.729573,
        source="dashboard",
        notes="manual review",
        path=str(db_path),
    )
    stored = load_earnings_events(symbols=["INTC"], path=str(db_path))

    assert round(float(stored.iloc[0]["surprise_pct"]), 1) == 100.0
    assert "manual review" in stored.iloc[0]["notes"]
    assert "raw_surprise_pct=1428.73" in stored.iloc[0]["notes"]


def test_fetch_fmp_earnings_events_uses_api_key_and_parser(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "symbol": "NVDA",
                    "date": "2026-05-01",
                    "epsActual": 1.20,
                    "epsEstimated": 1.00,
                }
            ]

    def fake_get(url, params, timeout):
        calls.append((url, params, timeout))
        return Response()

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    parsed = fetch_fmp_earnings_events("nvda", date(2026, 4, 1), date(2026, 5, 31), api_key="key")

    assert calls[0][0] == "https://financialmodelingprep.com/stable/earnings"
    assert calls[0][1] == {"symbol": "NVDA", "apikey": "key"}
    assert round(float(parsed.iloc[0]["surprise_pct"]), 1) == 20.0


def test_fmp_plan_error_marks_source_and_redacts_key(tmp_path):
    db_path = tmp_path / "meta.db"

    def fetch_fmp_broken(symbol: str, start: date, end: date) -> pd.DataFrame:
        raise RuntimeError(
            "402 Client Error: Payment Required for url: "
            "https://financialmodelingprep.com/stable/earnings?symbol=NVDA&apikey=secret"
        )

    fetch_fmp_broken.__name__ = "fetch_fmp_broken"
    report = import_earnings_events(
        ["NVDA"],
        "2026-04-01",
        "2026-05-31",
        path=str(db_path),
        fetcher=fetch_fmp_broken,
    )

    assert report.iloc[0]["status"] == "PLAN_LIMIT"
    assert report.iloc[0]["source"] == "fmp"
    assert "secret" not in report.iloc[0]["reason"]
    assert "apikey=REDACTED" in report.iloc[0]["reason"]


def test_fmp_provider_access_error_is_plan_limit(tmp_path):
    db_path = tmp_path / "meta.db"

    def fetch_fmp_broken(symbol: str, start: date, end: date) -> pd.DataFrame:
        raise ProviderAccessError("FMP endpoint or symbol requires a paid plan: NVDA")

    fetch_fmp_broken.__name__ = "fetch_fmp_broken"
    report = import_earnings_events(
        ["NVDA"],
        "2026-04-01",
        "2026-05-31",
        path=str(db_path),
        fetcher=fetch_fmp_broken,
    )

    assert report.iloc[0]["status"] == "PLAN_LIMIT"


def test_import_earnings_events_from_csv_dry_run_and_commit(tmp_path):
    csv_path = tmp_path / "earnings.csv"
    db_path = tmp_path / "meta.db"
    csv_path.write_text(
        "symbol,report_date,fiscal_period,eps_actual,eps_estimate,surprise_pct,source,notes\n"
        "NVDA,2026-05-01,2026-Q1,1.20,1.00,,manual_csv,reviewed\n"
    )

    dry_run = import_earnings_events_from_csv(csv_path, path=str(db_path))
    assert dry_run.iloc[0]["status"] == "DRY_RUN"
    assert load_earnings_events(path=str(db_path)).empty

    committed = import_earnings_events_from_csv(csv_path, commit=True, path=str(db_path))
    stored = load_earnings_events(symbols=["NVDA"], path=str(db_path))

    assert committed.iloc[0]["status"] == "WRITTEN"
    assert len(stored) == 1
    assert round(float(stored.iloc[0]["surprise_pct"]), 1) == 20.0


def test_import_earnings_events_from_csv_preserves_existing_without_overwrite(tmp_path):
    csv_path = tmp_path / "earnings.csv"
    db_path = tmp_path / "meta.db"
    csv_path.write_text(
        "symbol,report_date,surprise_pct,source,notes\n"
        "NVDA,2026-05-01,20.0,manual_csv,first\n"
    )
    import_earnings_events_from_csv(csv_path, commit=True, path=str(db_path))

    csv_path.write_text(
        "symbol,report_date,surprise_pct,source,notes\n"
        "NVDA,2026-05-01,-10.0,manual_csv,second\n"
    )
    exists = import_earnings_events_from_csv(csv_path, commit=True, path=str(db_path))
    stored = load_earnings_events(symbols=["NVDA"], path=str(db_path))

    assert exists.iloc[0]["status"] == "EXISTS"
    assert round(float(stored.iloc[0]["surprise_pct"]), 1) == 20.0

    overwritten = import_earnings_events_from_csv(
        csv_path,
        commit=True,
        overwrite=True,
        path=str(db_path),
    )
    stored = load_earnings_events(symbols=["NVDA"], path=str(db_path))
    assert overwritten.iloc[0]["status"] == "WRITTEN"
    assert round(float(stored.iloc[0]["surprise_pct"]), 1) == -10.0
