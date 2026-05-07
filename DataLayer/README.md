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

The familiar operator commands still work from `DataLayer/quantamental`.
Editable installs also expose package entry points from `DataLayer`.

Run the full pipeline:

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer/quantamental
python scripts/daily_pipeline.py --step all
```

Hardened package-style equivalent:

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer
quantamental-pipeline --step all
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

Other package entry points:

```bash
quantamental-check-data --days 60
quantamental-build-universe --stage static
quantamental-manage-candidates --show
```

## Alpha Engine

Run the V1 long-only alpha ranker for the AI-infra candidate universe:

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer/quantamental
python scripts/run_alpha.py --asof 2026-04-29
```

By default this saves Parquet/CSV ranking artifacts under `data/parquet/alpha/`
for review and dashboard display. It does not mutate QuestDB unless explicitly
requested:

```bash
python scripts/run_alpha.py --asof 2026-04-29 --persist-db
```

Backtest the ranker against `SPY`, `QQQ`, `SMH`, and an equal-weight candidate
basket:

```bash
python scripts/backtest_alpha.py --start 2025-01-01 --end 2026-04-01
```

Editable installs also expose:

```bash
quantamental-run-alpha --asof 2026-04-29
quantamental-backtest-alpha --start 2025-01-01 --end 2026-04-01
```

Generate the fund-manager style forward-performance report:

```bash
python scripts/alpha_performance.py --start 2025-01-01 --end 2026-04-01
```

This report tracks whether `TOP_BUY` and `BUY` buckets actually beat `SMH`
and the equal-weight candidate basket over 20/40 trading-day horizons. Saved
reports live under `data/parquet/alpha/performance/`.

Treat the ranker as decision-support until a walk-forward backtest shows that
the signal improves forward returns versus the baselines.

## Hardening Notes

Before treating this as production infrastructure:

- Initialize git and make a clean first commit from source files only.
- Keep databases, logs, caches, and local notebooks out of version control.
- Prefer adding tests around every signal rule before changing thresholds.
- Split large modules once they become painful to review, especially
  the dashboard panel layer and QuestDB read/write/schema modules.
