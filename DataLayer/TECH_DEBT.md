# Tech Debt Log

> Defects identified in the Apr 25 2026 system audit. **HIGH-priority items (D1, D2, D3) have been fixed.** This file tracks the remaining MEDIUM and LOW items so they can be picked up later. Each entry is self-contained — when you come back, you can implement any item without re-reading the whole codebase.

**Status legend**: 🔴 not started · 🟡 in progress · 🟢 fixed
**Priority**: 🟠 MEDIUM (spec compliance / robustness) · 🟢 LOW (quality of life)

---

## 🟠 D11 — Automate earnings event import for PEAD

**Status**: 🔴 not started
**Severity**: MEDIUM
**Files**: `quantamental/scripts/import_earnings_events.py`, `quantamental/signals/earnings.py`

### Problem
PEAD now works when earnings events are manually logged into SQLite, but the
operator still has to look up EPS actual, EPS estimate, report date, and
surprise percentage by hand. Manual loading is acceptable for testing, but it
does not scale across the AI-infra candidate universe during earnings season.

### Proposed fix
1. Add `scripts/import_earnings_events.py`.
2. Source first-pass data from `yfinance` earnings APIs.
3. Support:
   - `--tickers NVDA AMD MSFT`
   - `--candidate-list`
   - `--start YYYY-MM-DD --end YYYY-MM-DD`
   - `--limit N`
   - `--dry-run`
4. Insert only rows with report date and either:
   - explicit surprise percentage, or
   - both reported EPS and estimated EPS.
5. Upsert into `earnings_events` with `source='yfinance'`.
6. Print a review report: inserted, updated, skipped missing data, failed
   tickers.

### Acceptance
- `--dry-run` prints events without mutating SQLite.
- Re-running the importer is idempotent.
- Manually corrected rows are not overwritten unless `--overwrite` is passed.
- After import, `python scripts/diagnose_alpha.py --window ...` shows PEAD with
  active rank dates instead of `NO_VARIATION`.

### Estimated effort
2–4 hours

---

## 🟠 D4 — `validate_ohlcv` only warns, doesn't filter or persist flags

**Status**: 🔴 not started
**Severity**: MEDIUM
**Files**: `quantamental/data/ingest/polygon_client.py`

### Problem
Spec §9.1 says "flag any daily returns exceeding 20% for manual review." The current `validate_ohlcv()` function logs a warning but **still writes the bad row to QuestDB**. There is no record of which rows were flagged, so manual review is impossible after the fact.

### Current behavior
```python
def validate_ohlcv(df):
    suspicious = df[df["daily_return"].abs() > 0.20]
    if not suspicious.empty:
        logger.warning("Suspicious returns detected:\n%s", suspicious)
    return df.drop(columns=["prev_close", "daily_return"])  # returns ALL rows
```

### Proposed fix
1. Add a new QuestDB table `data_quality_warnings(ts, symbol, return_pct, action)`
2. Make `validate_ohlcv` return `(clean_df, flagged_df)` tuple
3. Persist flagged rows to the new table for review
4. Decision: keep writing to `daily_ohlcv` anyway (data is real), but include a flag column or sidecar

### Acceptance
- Re-running pipeline with a known synthetic >20% move shows up in `data_quality_warnings`
- Daily check (`scripts/check_data.py`) surfaces flagged rows in its report

### Estimated effort
1–2 hours

---

## 🟠 D5 — 2-day regime confirmation NOT implemented (spec violation)

**Status**: 🔴 not started
**Severity**: MEDIUM
**Files**: `quantamental/signals/aggregator.py`, `quantamental/data/ingest/questdb_writer.py`

### Problem
Spec §10 explicitly mitigates "Signal false positive" risk with: "Require 2+ consecutive days of new regime before acting." This is **not implemented anywhere**. A 1-day regime flip from `RISK_ON` to `RISK_OFF` would trigger Panel A's red badge and influence trading immediately.

### Current behavior
`run_and_store()` in `signals/aggregator.py` writes `regime` directly from today's composite score. No memory of yesterday.

### Proposed fix
1. Add `confirmed_regime STRING` column to `regime_signals` table (schema migration)
2. In `run_and_store()`, after computing `regime`:
   - Read yesterday's `regime` from DB
   - If today's `regime == yesterday's regime`: `confirmed_regime = today's regime`
   - Else: `confirmed_regime = yesterday's confirmed_regime` (carry forward)
3. Update dashboard Panel A to display `confirmed_regime`, with a small badge "(unconfirmed: NEW_REGIME)" when they differ
4. Update `check_data.py` to surface unconfirmed regime changes

### Schema change
```sql
ALTER TABLE regime_signals ADD COLUMN confirmed_regime STRING;
```
Or recreate the table with the column included if there's no production data worth preserving yet.

### Acceptance
- Insert 2 synthetic days of `RISK_ON` followed by 1 day of `RISK_OFF` — `confirmed_regime` should still read `RISK_ON`
- A second day of `RISK_OFF` flips `confirmed_regime` to `RISK_OFF`
- Tests in `tests/test_signals.py` cover this transition

### Estimated effort
2–3 hours (includes schema migration + dashboard update + tests)

---

## 🟠 D6 — FRED client refetches full history every run

**Status**: 🟡 partially mitigated (D1 fix added 90-day cap)
**Severity**: MEDIUM (downgraded after D1)
**Files**: `quantamental/data/ingest/fred_client.py`, `quantamental/scripts/daily_pipeline.py`

### Problem (original)
`fetch_all_macro()` was called with no bounds, pulling years of FRED history every day. Combined with the original `write_macro` (no dedup), this caused row duplication.

### Current state
- D1 fixed the duplication issue at the writer level
- D1 efficiency fix added: pipeline now passes `start=date.today() - 90 days` to FRED for `fetch_macro`, and `start=date.today() - 365 days` for `calc_signals`
- **Remaining issue**: FRED's `fetch_series` still does no incremental tracking — just respects whatever bounds the caller passes

### What's still wrong
The FRED client should track its own `last_fetched_ts` per series and only request observations newer than that. Right now if you run the pipeline twice in a day, both runs pull the same 90 days from FRED (only the WRITE is deduped).

### Proposed fix
1. Add `last_fetched_at` SQLite metadata table (or use existing meta.db)
2. `fetch_series(series_id)` defaults to `start = last_fetched_at(series_id) - 1 day` (small overlap for safety)
3. Update `last_fetched_at(series_id)` after successful fetch

### Acceptance
- Run pipeline at 5pm, then 5:30pm — FRED API calls in the second run should fetch only the few hours of new data (in practice 0 new observations)
- Log line shows "FRED yield_10y: fetched 0 new observations"

### Estimated effort
1 hour

---

## 🟠 D7 — Stop-loss alerts not deduplicated

**Status**: 🔴 not started
**Severity**: MEDIUM
**Files**: `quantamental/portfolio/stoploss.py`, `quantamental/portfolio/tracker.py` (SQLite schema)

### Problem
A position stuck just above its stop-loss for a week generates 7 identical Telegram alerts. Spammy and conditions the user to ignore them.

### Proposed fix
1. Add SQLite table `stop_loss_alerts(symbol, ts, distance_pct, sent)`
2. In `check_stops()`:
   - Before sending an alert, check the last alert for that symbol
   - Suppress if same alert within last 24h, **unless** `distance_pct` worsened (e.g. dropped from 4% to 2%, or breached)
3. Always log to console; only Telegram is rate-limited

### Acceptance
- Synthetic position 4% above stop → first run sends alert, second run silent
- Drop synthetic price so position is now 1% above stop → alert fires again (worsening)
- Re-run with no price change → silent

### Estimated effort
1–2 hours

---

## 🟢 D8 — `score_yield` uses absolute level thresholds (era-specific)

**Status**: 🔴 not started
**Severity**: LOW
**Files**: `quantamental/signals/macro.py`, `quantamental/config/settings.py`

### Problem
Hard-coded thresholds in `settings.py`:
```python
YIELD_STRONG_BULL_THRESHOLD = 4.0   # yield < 4% → strong bull
YIELD_STRONG_BEAR_THRESHOLD = 5.0   # yield > 5% → strong bear
```

In 2010, a 4% yield was elevated. In 2024, it's the new normal. These thresholds will misclassify across rate regimes.

### Proposed fix
Replace absolute thresholds with rolling percentiles:
- `Strong bullish`: 20MA below 60MA AND yield in bottom 25th percentile of last 5 years
- `Strong bearish`: 20MA above 60MA AND yield in top 25th percentile of last 5 years

### Implementation
```python
def score_yield(df: pd.DataFrame) -> int:
    series = df["value"].dropna()
    if len(series) < 252 * 5:  # need ~5 years for percentile
        # fallback to current absolute logic
        return _score_yield_absolute(df)

    ma_fast = series.rolling(20).mean().iloc[-1]
    ma_slow = series.rolling(60).mean().iloc[-1]
    latest = series.iloc[-1]
    p25 = series.tail(252 * 5).quantile(0.25)
    p75 = series.tail(252 * 5).quantile(0.75)

    if abs(ma_fast - ma_slow) <= YIELD_NEUTRAL_BAND_BPS: return 0
    if ma_fast < ma_slow: return 2 if latest < p25 else 1
    return -2 if latest > p75 else -1
```

### Acceptance
- Existing absolute-threshold tests still pass with synthetic short data
- New test: synthetic 5-year series at consistently elevated levels — strong bear NOT triggered just because yield is "high"

### Estimated effort
1–2 hours

---

## 🟢 D9 — `query()` SQL with literal `%` will fail

**Status**: 🔴 not started
**Severity**: LOW
**Files**: `quantamental/data/ingest/questdb_writer.py`

### Problem
SQLAlchemy's `text(sql)` interprets `%` as a parameter marker. Any query like:
```python
query("SELECT * FROM daily_ohlcv WHERE symbol LIKE 'NV%'")  # would error
```
will raise `tuple index out of range` or similar at execution time.

### Current code
```python
def query(sql: str) -> pd.DataFrame:
    engine = _get_engine()
    with engine.connect() as conn:
        return pd.read_sql_query(text(sql), conn)
```

### Proposed fix
Two options:
1. Auto-escape `%` → `%%` before passing to `text()`
2. Use `sqlalchemy.text(sql).execution_options(compiled_cache=None)` and bind parameters explicitly via `:param` placeholders, with an optional `params: dict` argument

Recommended: option 2 (better hygiene, prevents SQL injection if anyone ever uses dynamic input).

### Acceptance
- Add a test: `query("SELECT 'hello%' AS s")` should return one row with `s = 'hello%'`
- Add a test for parameterized usage: `query("SELECT * FROM x WHERE symbol = :sym", params={"sym": "NVDA"})`

### Estimated effort
30 min

---

## 🟢 D10 — Pipeline state file uses local-time `date.today()` not market time

**Status**: 🔴 not started
**Severity**: LOW
**Files**: `quantamental/scripts/daily_pipeline.py`

### Problem
`_state_path()` uses `date.today()`, which is system-local. If the user is in Europe and runs the pipeline at 11pm UTC, the state file is named after "today" while the NYSE-relevant trading day is yesterday.

### Current code
```python
def _state_path() -> Path:
    return Path(PIPELINE_LOG_PATH).parent / f"pipeline_state_{date.today()}.json"
```

### Proposed fix
```python
from zoneinfo import ZoneInfo
from datetime import datetime

def _market_today() -> date:
    """The current trading day in NYSE local time."""
    return datetime.now(ZoneInfo("America/New_York")).date()

def _state_path() -> Path:
    return Path(PIPELINE_LOG_PATH).parent / f"pipeline_state_{_market_today()}.json"
```

### Acceptance
- Mock `datetime.now` to a UK 11pm UTC time → `_market_today()` returns the previous calendar day
- Existing tests still pass (they pass tmp_path explicitly, bypassing this function)

### Estimated effort
30 min

---

## 🟢 D11 — `compute_pnl` propagates NaN if a price is missing

**Status**: 🔴 not started
**Severity**: LOW
**Files**: `quantamental/portfolio/tracker.py`

### Problem
```python
df["current_price"] = df["symbol"].map(latest_prices)
df["market_value"] = df["current_price"] * df["shares"]
```
If a symbol isn't in `latest_prices` (e.g. data fetch failed for that ticker), `current_price` is NaN, `market_value` is NaN, and the row silently shows blank P&L on the dashboard. Total P&L summary may also be wrong because NaN propagates through `df["pnl"].sum()` (pandas `sum()` skips NaN by default but the ROW shows blank).

### Proposed fix
1. Detect missing prices explicitly
2. Log a `WARNING` per missing symbol
3. Either skip the row in totals OR use `entry_price` as a fallback (with clear flag)
4. Dashboard Panel B should visually mark NaN rows

### Implementation sketch
```python
def compute_pnl(positions_df, latest_prices):
    if positions_df.empty:
        return positions_df
    df = positions_df.copy()
    df["current_price"] = df["symbol"].map(latest_prices)

    missing = df[df["current_price"].isna()]
    if not missing.empty:
        logger.warning("compute_pnl: missing prices for %s", list(missing["symbol"]))
        df["price_status"] = df["current_price"].apply(lambda x: "OK" if pd.notna(x) else "MISSING")

    # ... rest unchanged
    return df
```

### Acceptance
- Test: positions with NVDA + MSFT, prices dict has only NVDA → log warning, NVDA row shows P&L, MSFT row shows MISSING

### Estimated effort
30 min

---

## 🟠 D12 — Research universe has survivorship + look-ahead bias (BLOCKER for backtesting)

**Status**: 🔴 not started
**Severity**: MEDIUM (HIGH if/when backtesting begins)
**Files**: `quantamental/research/universe_builder.py`, `quantamental/data/ingest/polygon_client.py`, `quantamental/scripts/build_universe.py`

### Problem
`research/universe_builder.py:fetch_sp1500_from_wikipedia()` returns the **current** S&P 1500 constituents only. When applied retroactively to past data (e.g. for a 2024–2026 backtest), this introduces two compounding biases:

1. **Survivorship bias** — companies that were delisted, acquired, or removed from the index are missing entirely. Real examples in the 2024–2026 window: WBA, AAP, FRC, SIVB. A backtest never "sees" these failures, inflating measured strategy returns.
2. **Look-ahead bias** — using membership-as-of-today to define the universe-as-of-2024 means we're using future knowledge that "these companies survived to 2026." A real-time strategy in 2024 wouldn't have known.

**Magnitude**: typically inflates 2-year backtest returns by 1–4% annually for equity strategies; worse for small-cap (S&P 600 turnover) and volatility-chasing strategies.

**Why it's not blocking now**: live trading and current research only need today's universe (which Wikipedia provides correctly). Backtesting is the issue, and backtesting is **Month 2** per spec §11. So this is queued but not urgent.

### Tiered solutions (pick one when Month 2 starts)

#### Tier 1 — Free, ~80% bias eliminated (recommended)
Use Polygon's reference data to pull both active AND delisted US common stocks for the backtest window:

```python
# In universe_builder.py
def fetch_polygon_active_and_delisted(start_date: date) -> pd.DataFrame:
    """Pull all US common stocks that were active at any point since start_date.

    Includes currently-active stocks PLUS stocks delisted after start_date.
    Catches bankruptcies, mergers, delistings during the window.
    """
    from polygon import RESTClient
    from config.settings import POLYGON_API_KEY
    client = RESTClient(api_key=POLYGON_API_KEY)
    records = []

    # Active common stocks
    for t in client.list_tickers(market="stocks", active=True, type="CS", limit=1000):
        records.append({"symbol": t.ticker, "active": True, "delisted_utc": None})

    # Delisted common stocks
    for t in client.list_tickers(market="stocks", active=False, type="CS", limit=1000):
        delisted = getattr(t, "delisted_utc", None)
        if delisted and pd.Timestamp(delisted).date() >= start_date:
            records.append({"symbol": t.ticker, "active": False, "delisted_utc": delisted})

    return pd.DataFrame(records).drop_duplicates(subset="symbol")
```

What this catches: bankruptcies, mergers, delistings within the backtest window
What this misses: index membership changes (stock outside S&P 500 in 2024 but in it in 2026 is treated as always-in)

Cost: 0
Effort: 2–3 hours

#### Tier 2 — Paid, comprehensive ($50–300/yr)
| Source | Cost/yr | What you get |
|---|---|---|
| **Sharadar SF1** (via Quandl/Nasdaq) | ~$100 | Point-in-time fundamentals + index membership |
| **Norgate Data** | ~$300 | Full historical S&P constituents back to inception, delisted included |
| **SimFin Premium** | ~$50 | Some historical constituents |

Effort: 4–6 hours (data feed integration, schema changes for `index_membership(symbol, index, start_date, end_date)` table)

#### Tier 3 — Institutional ($$$$)
S&P direct, Bloomberg, FactSet, Refinitiv. Out of scope for retail.

### Proposed implementation (Tier 1)

1. Add `quantamental/research/historical_universe.py`:
   - `build_backtest_universe(start: date, end: date) -> list[dict]` — returns ticker + active period
   - Combines current S&P 1500 + Polygon delisted tickers
2. Add new QuestDB table `ticker_metadata`:
   ```sql
   CREATE TABLE ticker_metadata (
       symbol SYMBOL CAPACITY 4096 INDEX,
       active BOOLEAN,
       delisted_date DATE,
       sector STRING,
       last_updated TIMESTAMP
   ) TIMESTAMP(last_updated);
   ```
3. Backtest engine (Month 2) filters universe by `(active = true) OR (delisted_date >= backtest_date)` for each backtest day
4. Document remaining bias in every backtest result header (e.g. "Universe: S&P 1500 current + Polygon delisted since 2024-06-01. NOT point-in-time index membership — see TECH_DEBT.md D12.")

### Acceptance
- `python scripts/build_universe.py --stage backtest --start 2024-06-01` writes a JSON file with both active and recently-delisted tickers
- Loading shows ~1,300 active + ~50–100 delisted (rough estimate based on typical delisting frequency)
- Backtest results header explicitly notes the remaining bias

### Estimated effort
**Tier 1**: 2–3 hours
**Tier 2** (with paid feed): 4–6 hours

### Recommendation
Don't implement until Month 2 backtest engine work begins. Tier 1 is the right starting point — eliminates ~80% of bias for $0. Upgrade to Tier 2 only if backtests show suspicious results that warrant deeper investigation.

---

## Total remaining effort estimate

| Item | Severity | Effort |
|---|---|---|
| D4 OHLCV validation | MEDIUM | 1–2h |
| D5 2-day regime confirm | MEDIUM | 2–3h |
| D6 FRED incremental | MEDIUM | 1h |
| D7 Alert dedup | MEDIUM | 1–2h |
| D8 Percentile yield | LOW | 1–2h |
| D9 `%` escape | LOW | 0.5h |
| D10 Market timezone | LOW | 0.5h |
| D11 NaN handling | LOW | 0.5h |
| **D12 Survivorship bias** | **MEDIUM** (Month 2 blocker) | **2–3h** |
| **Total** | | **~11–15h** |

Recommended order if/when picked up:

1. **D5** (highest spec-compliance value, regime stability)
2. **D11, D10, D9** (quick wins, ~1.5h combined)
3. **D7** (user-visible quality)
4. **D4, D6** (data hygiene)
5. **D8** (analytical refinement, depends on having multi-year data first)
6. **D12** (do this BEFORE starting any Month 2 backtest work — it's a hard prerequisite for valid backtest results)

---

## How to use this file

When you decide to tackle any item:

1. Pick one item from above
2. Tell Claude: "Let's fix D5" (or whichever number)
3. Claude reads this file, the referenced source files, and writes a focused implementation
4. After fix lands: change `Status: 🔴 not started` → `Status: 🟢 fixed (YYYY-MM-DD)`, add a brief `Resolution:` line at the bottom of that section

---

## 🟢 D14 — Fundamentals scope reduced to candidate list

**Status**: 🟢 resolved (2026-04-27)
**Severity**: design decision (not a defect)
**Files**: `quantamental/scripts/backfill_fundamentals.py`, `quantamental/data/ingest/yfinance_fundamentals.py`, `quantamental/scripts/daily_pipeline.py`

### Context
Fundamentals were originally backfilled across all ~1,386 research-universe tickers. yfinance hits Yahoo IP-level rate limits aggressively at that scale (45+ min runs, frequent empty-body throttling). More importantly: **no signal currently consumes the `fundamentals` QuestDB table** — PEAD reads earnings events from SQLite, technicals don't need fundamentals at all. We were paying a steep cost for unused data.

### Decision
Scope `backfill_fundamentals.py` default to the **candidate list** (~26 tickers, ~30s, reliable). The full research universe remains opt-in via `--research-universe` for the rare case someone is prototyping a fundamental factor scan. Future fundamental-driven signals (valuation overlays, fundamental momentum, leverage filters) only need to fire on candidates anyway, since that's the trading universe.

### Resolution
- `backfill_fundamentals.py`: candidate list is now the default; `--research-universe` flag for full sweep (auto-applies 45s batch pauses).
- `yfinance_fundamentals.py`: default `batch_pause=0` (irrelevant under 100 tickers); CLI auto-bumps to 45s when `--research-universe` is selected.
- `daily_pipeline.py`: new `refresh_fundamentals` step runs every Monday on the candidate list (~30s, idempotent, skipped silently other days).
- Candidate list editing now has three sync'd paths: CLI (`manage_candidates.py`), dashboard Panel E, direct JSON edit. All write through `config.universe.save_candidate_list()`.

### Revisit if
A future signal needs fundamentals across the full S&P 1500 (e.g. systematic factor scoring). At that point: bulk-backfill once with `--research-universe`, then rely on weekly Monday candidate refreshes plus quarterly full-universe top-ups.
