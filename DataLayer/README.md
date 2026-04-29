# Quantamental Data Layer

Local quantamental research and operating system for market data ingestion,
macro/sector/stock signal scoring, portfolio tracking, and a Streamlit
dashboard.

This repository is intentionally operator-friendly: the deeper daily workflow
lives in `USER_MANUAL.md`, and Month 2 signal operations live in
`MONTH_2_HANDBOOK.md`.

## Project Shape

```text
quantamental/
  config/       Runtime settings, candidate lists, signal registry
  data/         QuestDB/FRED/Polygon/yfinance ingestion helpers
  dashboard/    Streamlit dashboard
  portfolio/    SQLite-backed portfolio, journal, and stop-loss logic
  research/     Universe construction and filtering
  scripts/      Daily pipeline and operator CLIs
  signals/      Macro, sector, stock, and composite signal logic
  tests/        Unit and integration-style tests
```

Runtime data belongs in ignored locations such as `quantamental/data/meta.db`,
`quantamental/data/parquet/`, and `quantamental/logs/`. Secrets belong in
`quantamental/config/.env`; commit only `quantamental/config/.env.example`.

## Setup

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp quantamental/config/.env.example quantamental/config/.env
```

Fill in `POLYGON_API_KEY` and `FRED_API_KEY` in `quantamental/config/.env`.

Start QuestDB:

```bash
docker compose up -d
```

## Daily Commands

Run the full pipeline:

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer/quantamental
python scripts/daily_pipeline.py --step all
```

Open the dashboard:

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer/quantamental
streamlit run dashboard/app.py
```

Run tests:

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer
python -m pytest
```

## Hardening Notes

Before treating this as production infrastructure:

- Initialize git and make a clean first commit from source files only.
- Keep databases, logs, caches, and local notebooks out of version control.
- Prefer adding tests around every signal rule before changing thresholds.
- Split large modules once they become painful to review, especially
  `dashboard/app.py` and `data/ingest/questdb_writer.py`.
