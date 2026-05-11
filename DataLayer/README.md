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

## Dashboard Reading Guide

The dashboard is a decision-support cockpit, not an auto-trading screen. Read it
in this order:

1. **Overview** — start here. It combines current macro regime, sector score,
   data freshness, latest alpha leader, target exposure, portfolio drift, and
   the next action queue. If this page says `BLOCKED`, `De-risk`, or
   `Selective only`, treat all buy ideas as research candidates rather than
   orders.
2. **Alpha** — check the ranked stock book and validation evidence. `Alpha
   Book` shows today's ranked `TOP_BUY`, `BUY`, `HOLD`, and `AVOID` buckets plus
   target weights. `Alpha Validation` shows whether those buckets have actually
   beaten `SMH` and the equal-weight candidate basket over forward 20/40 trading
   days.
3. **Signals** — inspect why the system is risk-on or risk-off. Macro signals
   explain liquidity/risk appetite; sector signals explain AI-infra cycle
   strength; stock signals explain ticker-level momentum, volume, RSI, and PEAD.
4. **Portfolio** — compare current positions against target weights, current
   P&L, and stop-loss distance before acting.
5. **Universe** — edit the candidate list. This controls which tickers the
   alpha engine ranks and which names appear in the dashboard decision flow.

Key interpretation rules:

- `TRUSTED` means OHLCV, stock signals, macro/sector signals, and alpha ranks
  are fresh enough for live decision support. `BLOCKED` means do not trust live
  ranks until the listed data issue is fixed.
- `TOP_BUY` is only useful if validation is healthy. A negative `top-vs-avoid`
  spread means the ranker has not recently separated winners from avoid names.
- `Rank IC` measures whether higher ranks tend to produce better forward
  returns. Positive is good; near zero means weak ordering power.
- `RISK_OFF` blocks new long allocations in the V1 portfolio logic and should
  push the system toward cash.
- Negative sector composite caps deployment even when individual stocks look
  strong.
- Target weights are portfolio suggestions under the model assumptions, not
  trade instructions.

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

Diagnose which alpha components are helping or hurting:

```bash
python scripts/diagnose_alpha.py --start 2025-01-01 --end 2026-04-01
```

This writes component IC, top/bottom spread, bucket attribution, and review
recommendations under `data/parquet/alpha/diagnostics/`. Use this before
changing ranker weights; a weak live ranker needs evidence about which signal is
bad, not just a new score formula.

Run multi-window diagnostics before trusting a ranker change:

```bash
python scripts/diagnose_alpha.py \
  --window 2025-01-01:2026-04-01 \
  --window 2025-07-01:2026-04-01
```

PEAD uses EPS surprise events from SQLite. Import recent earnings events in
the dashboard Alpha tab, or import recent earnings events in review mode first
and commit only after the rows look right.

Dashboard entry:

1. Open the Alpha tab.
2. In `PEAD Events`, enter ticker, report date, surprise percentage or EPS
   actual/estimate, source, and notes.
3. Save the event, then rerun stock signals/alpha so the ranker can consume it.

ETFs and benchmarks are separated from single-name equities. The Alpha and PEAD
workflows use the single-name equity list by default; ETF instruments are shown
in their own dashboard tab.

Command-line import:

```bash
python scripts/import_earnings_events.py \
  --tickers NVDA AMD MSFT \
  --from 2026-04-01 \
  --to 2026-05-09

python scripts/import_earnings_events.py \
  --tickers NVDA AMD MSFT \
  --from 2026-04-01 \
  --to 2026-05-09 \
  --commit
```

To try Financial Modeling Prep instead of yfinance, add `FMP_API_KEY` to
`quantamental/config/.env`, then run the same importer with `--provider fmp`:

```bash
python scripts/import_earnings_events.py \
  --provider fmp \
  --tickers NVDA AMD MSFT \
  --from 2026-04-01 \
  --to 2026-05-09

python scripts/import_earnings_events.py \
  --provider fmp \
  --tickers NVDA AMD MSFT \
  --from 2026-04-01 \
  --to 2026-05-09 \
  --commit
```

Without `--tickers`, the importer scans the active candidate list. The default
mode writes nothing; `--commit` upserts reviewable `READY` rows into
`earnings_events`. Existing symbol/report-date rows are preserved unless you
also pass `--overwrite`.

PEAD surprise values are winsorized at ±100 percentage points before storage.
If a provider returns an extreme value, for example from a tiny EPS estimate,
the stored `surprise_pct` is capped and the raw value is preserved in `notes`.
PEAD is currently **observe-only** in the V1 alpha book: events are collected
and displayed, but `pead_signal` has zero direct alpha weight until source
quality is validated.

If yfinance rate-limits, use a reviewed CSV instead:

```csv
symbol,report_date,fiscal_period,eps_actual,eps_estimate,surprise_pct,source,notes
NVDA,2026-05-01,2026-Q1,1.20,1.00,,manual_csv,reviewed
AMD,2026-05-06,2026-Q1,,,6.25,manual_csv,reviewed
```

```bash
python scripts/import_earnings_events_csv.py --file earnings.csv
python scripts/import_earnings_events_csv.py --file earnings.csv --commit
```

You can still log surprises manually in percent points, then rerun stock
signals or alpha diagnostics:

```bash
python scripts/log_earnings_event.py \
  --symbol NVDA \
  --report-date 2026-05-01 \
  --fiscal-period 2026-Q1 \
  --surprise-pct 12.5 \
  --source "company release"

python scripts/log_earnings_event.py --show
```

You can also provide actual and consensus EPS instead of `--surprise-pct`:

```bash
python scripts/log_earnings_event.py \
  --symbol NVDA \
  --report-date 2026-05-01 \
  --eps-actual 1.20 \
  --eps-estimate 1.00
```

Treat the ranker as decision-support until a walk-forward backtest shows that
the signal improves forward returns versus the baselines.

## Hardening Notes

Before treating this as production infrastructure:

- Initialize git and make a clean first commit from source files only.
- Keep databases, logs, caches, and local notebooks out of version control.
- Prefer adding tests around every signal rule before changing thresholds.
- Split large modules once they become painful to review, especially
  the dashboard panel layer and QuestDB read/write/schema modules.
