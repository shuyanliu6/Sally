# Quantamental Tech Debt And Hardening Roadmap

Last reviewed: 2026-05-11

This file is the current hardening backlog for the quantamental AI-infra
research system. It combines the existing project audit with the latest
system-review report.

Important scope: this is a **single-operator research / decision-support
system**. Trades are executed manually. Therefore the main failure mode is not
an accidental automated order; it is a wrong or stale number causing a wrong
human decision.

The project is useful today as a research console. It is not yet a reliable
alpha engine. The next work should improve research integrity, dashboard
truthfulness, and validation quality before adding more factors.

## Severity

- 🔴 Critical: silently misleads research or invalidates backtests
- 🟠 High: correctness / integrity / trust issue
- 🟡 Medium: operational reliability or dashboard clarity
- 🟢 Low: hygiene, maintainability, polish

## Status

- 🔴 not started
- 🟡 partial / in progress
- 👀 observe-only
- 🟢 fixed / accepted

---

## Executive Summary

### What Is Working

- The project has a coherent pipeline: OHLCV, macro, sector, stock signals,
  alpha ranks, portfolio monitor, and dashboard.
- Freshness guardrails exist.
- ETFs/benchmarks are separated from single-name equities.
- PEAD is collected and displayed, but no longer affects alpha ranking.
- FMP/yfinance/CSV/manual earnings importers are review-first and idempotent.
- The test suite is broad and currently healthy; latest user run showed
  `274 passed`.

### What Is Not Reliable Enough Yet

- Backtests are not yet believable enough for capital-allocation confidence.
- A few point-in-time and survivorship edges can inflate measured alpha.
- Dashboard truthfulness is incomplete: some tabs can still show stale values
  without a strong banner or explicit as-of timestamp.
- Several code paths convert broken/missing data into neutral-looking zeros.
- Manual or provider-sourced signals can become stale or disagree without a
  review workflow.
- Active factor weights are still heuristic rather than validation-gated.

### Recommended Fix Order

1. Dashboard truthfulness: global freshness banner and as-of timestamps.
2. Research-integrity blockers: leakage, costs, delistings, walk-forward tests.
3. Data-quality auditability: persistent OHLCV warnings and missing-price flags.
4. Dead-signal visibility: distinguish neutral from broken.
5. PEAD source governance before any future reactivation.
6. Dashboard workflow separation.
7. Operational hygiene and cleanup.

---

# 🔴 Critical — Research Integrity And Dashboard Truthfulness

## D1 — PEAD Cutoff Can Leak Same-Day Earnings Prints

**Status**: 🔴 not started
**Files**: `quantamental/alpha/features.py`,
`quantamental/signals/stock.py`, `quantamental/tests/test_alpha_engine.py`

### Problem

Earnings often print after market close. If a PEAD event with `report_date = T`
is allowed into features for `asof = T`, a backtest or intraday run may use
information that was not tradable at the signal time.

PEAD is currently observe-only in alpha scoring, but the leakage should still
be fixed before PEAD is evaluated or reactivated.

### Proposed Fix

Use a strict availability rule:

- either `event_date < asof_date`;
- or better, `event_available_session <= asof_session`, where after-close
  events become available on the next trading day.

Add a test with an earnings event on day `T`:

- `asof=T` should not see the event;
- `asof=T+1 trading day` should see it.

### Acceptance

- Synthetic same-day earnings event is excluded from `asof=T` features.
- Dashboard PEAD panel can still display the event separately as reported data.

### Effort

1 hour

---

## D2 — Backtest Is Not Yet Fund-Manager Grade

**Status**: 🔴 not started
**Files**: `quantamental/alpha/backtest.py`,
`quantamental/alpha/performance.py`, `quantamental/alpha/diagnostics.py`,
`quantamental/scripts/backtest_alpha.py`,
`quantamental/scripts/alpha_performance.py`

### Problem

The current backtest is useful for smoke testing, but not enough to trust an
alpha engine:

- no true walk-forward parameter selection;
- no train/validate/out-of-sample split;
- factor weights are hand-set, not selected on prior windows;
- `avg_holding_period_days` is hard-coded;
- turnover is not analyzed by rebalance/name;
- performance is not conditioned by macro/sector regime;
- backtest reports should be treated as upper bounds until leakage,
  survivorship, and cost-reporting consistency issues are fixed.

### Proposed Fix

1. Add a walk-forward CLI that sweeps rolling windows and reports IC
   distribution, not a single number.
2. Add factor ablation:
   - baseline;
   - baseline + factor;
   - rank IC delta;
   - bucket-spread delta;
   - drawdown / turnover impact.
3. Measure holding period from actual entry/exit dates.
4. Report performance by regime.
5. Add a PASS / WATCH / FAIL validation status consumed by the dashboard.

### Acceptance

- Backtest output includes measured holding period, turnover by rebalance, and
  walk-forward windows.
- Alpha Validation dashboard shows whether the current alpha book is supported
  by validation.
- Fixed-seed / fixed-data backtest output is reproducible.

### Effort

4-8 hours

---

## D3 — Transaction Costs Need Shared Configuration And Clear Reporting

**Status**: 🟡 partial / in progress
**Files**: `quantamental/alpha/backtest.py`,
`quantamental/alpha/performance.py`,
`quantamental/config/settings.py`

### Problem

Backtest strategy returns already include a `transaction_cost_bps` parameter,
and tests cover that costs reduce returns. The remaining issue is consistency:
the cost assumption is not centralized, and dashboard / performance reports do
not clearly label whether the validation view is gross or net of costs.

Month-2 assumptions call for realistic slippage/impact. Weekly rebalancing
across 8-12 names can make gross alpha look better than it is, so the user
should never have to infer which cost model a validation number uses.

### Proposed Fix

Add one shared setting, for example:

```python
TXN_COST_BPS_ROUNDTRIP = 20.0
```

Use it consistently in:

- backtest returns;
- alpha performance;
- dashboard validation summary.

### Acceptance

- Backtest A vs backtest A plus cost shows return drop of roughly
  `turnover * cost`.
- Dashboard validation explicitly says gross or net.

### Effort

1-2 hours

---

## D4 — Delistings / Disappearing Names Bias Backtests

**Status**: 🔴 not started
**Files**: `quantamental/alpha/features.py`,
`quantamental/alpha/backtest.py`,
`quantamental/research/universe_builder.py`

### Problem

If a name delists or disappears from OHLCV/stock-signal coverage, it can vanish
from the feature/pivot universe. In historical testing this avoids realizing
losses and compounds survivorship bias.

This is related to point-in-time universe construction, but specifically covers
how delisted names are carried through portfolio exit.

### Proposed Fix

1. Build a backtest universe with active periods.
2. Carry delisted names until the next rebalance.
3. Apply final-price floor or explicit delisting return policy.
4. Document the policy in every backtest report.

### Acceptance

- Synthetic delisting mid-window remains in the portfolio until exit logic
  realizes the loss.
- Backtest report states how delistings were handled.

### Effort

3-5 hours for Tier 1 implementation

---

## D5 — Dashboard Freshness Gate Is Not Global Enough

**Status**: 🔴 not started
**Files**: `quantamental/dashboard/app.py`,
`quantamental/dashboard/freshness.py`,
`quantamental/dashboard/panels.py`

### Problem

Freshness gating currently exists, but the dashboard can still show important
numbers outside the Alpha tab without a strong global warning. Overview,
Portfolio, ETF, and Signals views can appear authoritative even when the
underlying data is stale or blocked.

### Proposed Fix

1. Render freshness status at the top of every tab.
2. If freshness is `BLOCKED`, visually mark all decision/rank/portfolio panels
   as not trusted.
3. Keep research/data-ops panels visible, but clearly label the data state.

### Acceptance

- Force freshness `BLOCKED` in a test and assert all tabs render a warning.
- No rank/price/portfolio panel appears without a trust state.

### Effort

1-2 hours

---

## D6 — No As-Of Timestamp On Many Dashboard Values

**Status**: 🔴 not started
**Files**: `quantamental/dashboard/panels.py`,
`quantamental/dashboard/data.py`, `quantamental/alpha/reporting.py`

### Problem

The dashboard shows prices, ranks, signals, and portfolio values without
consistently stating:

- price as-of time;
- alpha-rank as-of date;
- signal calculation date;
- fetch time of cached dashboard data.

For a manual research system, this is one of the cheapest and highest-value
truthfulness improvements.

### Proposed Fix

Add panel-level as-of lines:

```text
Prices as of 2026-05-09 16:00 ET · Alpha computed 2026-05-10 09:42 China
```

### Acceptance

- Alpha Book, Overview, Portfolio, Signals, and ETF panels show as-of metadata.
- Dashboard cached fetch time is visible in the freshness panel.

### Effort

1-2 hours

---

## D7 — Position PnL Can Under-Report When Prices Are Missing

**Status**: 🔴 not started
**Files**: `quantamental/portfolio/tracker.py`,
`quantamental/dashboard/panels.py`, `quantamental/tests/test_portfolio.py`

### Problem

If a held position is missing a latest price, PnL rows can become NaN. Pandas
can skip NaNs in totals, causing the dashboard total to under-report or look
cleaner than reality.

### Proposed Fix

1. Add `price_status` to PnL rows:
   - `OK`;
   - `MISSING`;
   - `STALE`.
2. Style missing rows in red/amber.
3. Refuse to show a portfolio-level PnL total unless all open positions are
   priced, or show a prominent partial-total label.

### Acceptance

- Missing MSFT price with NVDA present shows NVDA row, MSFT `MISSING`, and no
  clean portfolio total.

### Effort

1 hour

---

# 🟠 High — Alpha And Signal Correctness

## D8 — Dead / Degenerate Components Look Like Healthy Neutral Signals

**Status**: 🔴 not started
**Files**: `quantamental/alpha/ranking.py`,
`quantamental/alpha/diagnostics.py`, `quantamental/dashboard/panels.py`

### Problem

`_percentile_component()` returns `0.0` when a component has insufficient data
or no variation. But `0.0` is also the expected value for a healthy neutral
factor. Downstream code cannot tell the difference between:

- a factor that is neutral;
- a factor that is dead;
- a factor that has no usable data.

### Proposed Fix

For each component, add state:

```text
OK
NO_VARIATION
INSUFFICIENT_DATA
MISSING
```

Surface this in:

- `score_components`;
- `diagnose_alpha`;
- Alpha Validation dashboard.

### Acceptance

- Constant synthetic factor produces `NO_VARIATION`, not just `0.0`.
- Dashboard shows dead/degenerate factors.

### Effort

1-2 hours

---

## D9 — Stale Features Still Contribute Full Feature Values

**Status**: 🔴 not started
**Files**: `quantamental/alpha/ranking.py`,
`quantamental/alpha/features.py`

### Problem

The ranker flags stale/no-price rows and applies a data-quality penalty. But
momentum, drawdown, volatility, and other stale-input-derived features can still
contribute their full component values.

### Proposed Fix

Choose one:

1. Exclude stale names from ranking entirely.
2. Keep them visible but zero out all price-derived feature components.
3. Force stale names to `AVOID` and make all non-quality components neutral.

### Acceptance

- Synthetic stale price older than threshold cannot benefit from stale momentum
  or drawdown.
- Stale names remain visible as `AVOID` for operator awareness.

### Effort

1 hour

---

## D10 — PEAD Decay And Quantization Are Not Ready For Reactivation

**Status**: 👀 observe-only
**Files**: `quantamental/signals/stock.py`,
`quantamental/signals/earnings.py`

### Problem

PEAD is currently disabled in alpha scoring, which is appropriate. Before it is
ever re-enabled:

- linear 28-day decay needs empirical support;
- integer rounding of decayed PEAD loses information;
- source governance must be implemented.

### Proposed Fix

1. Keep PEAD as float through stock aggregation.
2. Estimate decay curve from reviewed historical PEAD events.
3. Do not activate PEAD unless source-reviewed events show positive forward IC.

### Acceptance

- PEAD score can be float.
- PEAD diagnostics show decay-window sensitivity.
- Only `APPROVED` PEAD rows can be used for alpha tests.

### Effort

2-4 hours, after D19 source governance

---

## D11 — Portfolio Target Weights Ignore Conviction

**Status**: 🔴 not started
**Files**: `quantamental/alpha/portfolio.py`,
`quantamental/dashboard/panels.py`

### Problem

Selected names receive equal target weights. A score-75 `TOP_BUY` gets the same
suggested weight as a score-41 `HOLD`.

For manual execution this is suggestion quality, not an execution bug, but the
dashboard should provide a better starting point.

### Proposed Fix

Weight selected names by conviction:

```text
raw_weight = max(alpha_score - 50, 0)
normalize to deployment cap
clip to max_weight
respect min_weight where possible
```

### Acceptance

- Target weights are monotone in alpha score after clipping.
- Weights still respect deployment cap and single-name max.

### Effort

1-2 hours

---

## D12 — Macro And Sector Deployment Caps Do Not Compound

**Status**: 🔴 not started
**Files**: `quantamental/alpha/portfolio.py`

### Problem

Current deployment caps use the tighter of macro and sector caps. If macro is
risk-off and sector is negative, deployment still caps at 50%, not 35%.

For a manual system this is suggestion quality, but compounding caps gives a
clearer risk signal.

### Proposed Fix

Apply multiplicative caps:

```text
cap = macro_cap * sector_cap
```

### Acceptance

- Risk-off plus negative sector produces a stricter cap than either condition
  alone.

### Effort

30 minutes

---

## D13 — Bucket Thresholds Are Absolute Rather Than Adaptive

**Status**: 🔴 not started
**Files**: `quantamental/alpha/ranking.py`

### Problem

`TOP_BUY` and `BUY` use absolute alpha-score thresholds. In flat markets the
distribution can compress and produce no top buys; in hot markets it can
over-fire.

### Proposed Fix

Use rank-percentile gating plus recent score dispersion, or make thresholds
configurable and validate them by walk-forward tests.

### Acceptance

- Bucket counts are stable enough to be useful across flat and strong tapes.
- Thresholds are documented and validation-backed.

### Effort

1-2 hours

---

## D14 — Disabled Signal Renormalization Is Not Explained

**Status**: 🔴 not started
**Files**: `quantamental/signals/stock.py`,
`quantamental/config/signals_registry.yaml`, `quantamental/dashboard/panels.py`

### Problem

When a stock-level signal is disabled, the active signal set is renormalized.
This may be reasonable, but it changes effective weights and is not visible to
the user.

### Proposed Fix

Display effective stock-signal weights in the dashboard or diagnostics output.

### Acceptance

- Operator can see post-normalization stock-signal weights.

### Effort

1 hour

---

## D15 — Manual Sector Signals Can Stick Stale Indefinitely

**Status**: 🔴 not started
**Files**: `quantamental/signals/sector_ai_infra.py`,
`quantamental/signals/sector.py`, `quantamental/dashboard/panels.py`

### Problem

Manual sector inputs such as TSMC revenue, capex surprise, and API pricing can
remain active indefinitely if the user forgets to update them. A stale manual
row can silently freeze part of the sector composite.

### Proposed Fix

1. Add max-age policy per manual signal.
2. Return `STALE` state when max age is exceeded.
3. Penalize or neutralize stale manual signals.
4. Show amber/red badges in the Signals tab.

### Acceptance

- Synthetic stale TSMC/capex/API row becomes stale after configured max age.
- Dashboard clearly labels stale manual inputs.

### Effort

1-2 hours

---

# 🟡 Medium — Reliability And Operations

## D16 — Dashboard Cache Can Mask Data Outages

**Status**: 🔴 not started
**Files**: `quantamental/dashboard/data.py`,
`quantamental/dashboard/freshness.py`

### Problem

`@st.cache_data(ttl=60)` helps performance, but a cached snapshot can make a
database outage look temporarily healthy unless the fetch time and failure state
are surfaced.

### Proposed Fix

Return `(data, fetched_at, source_status)` or equivalent metadata from dashboard
loaders. Show cache/fetch time in freshness panel.

### Acceptance

- If QuestDB goes down, dashboard shows stale cached data as stale/cached, not
  fresh.

### Effort

1-2 hours

---

## D17 — Freshness Coverage Allows Silent Dropout

**Status**: 🔴 not started
**Files**: `quantamental/dashboard/freshness.py`,
`quantamental/scripts/check_data.py`

### Problem

The freshness gate allows 90% symbol coverage. In a 50-name universe, that can
hide five missing tickers unless the user digs deeper.

### Proposed Fix

1. List missing tickers in freshness detail.
2. Consider separate thresholds:
   - `WARN` if any active candidate missing;
   - `FAIL` if more than N missing or if missing ticker is held/ranked.
3. Make `check_data.py` print missing names explicitly.

### Acceptance

- Missing tickers are visible in dashboard and CLI health output.

### Effort

30-60 minutes

---

## D18 — Market Calendar Logic Is Split

**Status**: 🟡 partial
**Files**: `quantamental/dashboard/freshness.py`,
`quantamental/data/ingest/polygon_client.py`,
`quantamental/scripts/check_data.py`,
`quantamental/scripts/daily_pipeline.py`

### Problem

Market fetching uses an NYSE calendar when available, while dashboard freshness
and check-data paths still use weekday approximations in places. This can be
wrong around holidays and half-days.

### Proposed Fix

Create one shared calendar utility and use it everywhere:

- expected market date;
- previous trading day;
- pipeline state date;
- health checks;
- dashboard clock.

### Acceptance

- Holiday/weekend/pre-close/post-close tests agree across all modules.

### Effort

1-2 hours

---

## D19 — PEAD Source Governance Is Missing

**Status**: 👀 observe-only
**Files**: `quantamental/signals/earnings.py`,
`quantamental/signals/earnings_importer.py`,
`quantamental/dashboard/panels.py`

### Problem

Provider PEAD data can disagree materially because of fiscal-quarter mismatch,
GAAP vs adjusted EPS, consensus timing, or provider errors. The AMZN FMP vs
Nasdaq mismatch is the current example.

### Proposed Fix

Extend `earnings_events` with review metadata:

- `raw_surprise_pct`;
- `stored_surprise_pct`;
- `source_priority`;
- `review_status`;
- `reviewed_at`;
- `review_notes`.

Review statuses:

- `AUTO_IMPORTED`;
- `NEEDS_REVIEW`;
- `APPROVED`;
- `REJECTED`;
- `OVERRIDDEN`.

### Acceptance

- Imports default to `AUTO_IMPORTED`.
- Extreme/source-disagreed rows become `NEEDS_REVIEW`.
- Alpha can ignore non-approved PEAD rows.

### Effort

3-5 hours

---

## D20 — Universe Editor Needs Atomic Writes

**Status**: 🔴 not started
**Files**: `quantamental/config/universe.py`,
`quantamental/dashboard/panels.py`,
`quantamental/scripts/manage_candidates.py`

### Problem

Dashboard and pipeline can touch `candidate_list.json` around the same time.
The save helper writes directly rather than using an atomic temp-file replace
or file lock.

### Proposed Fix

Write to a temp file and `os.replace()` it into place. Optionally add file
locking for read/write paths.

### Acceptance

- Interrupted write cannot leave a partial JSON file.

### Effort

30-60 minutes

---

## D21 — Missing Secrets Should Fail Fast

**Status**: 🔴 not started
**Files**: `quantamental/config/settings.py`,
`quantamental/data/ingest/polygon_client.py`,
`quantamental/data/ingest/fred_client.py`

### Problem

Several secrets default to empty string. That can lead to confusing empty API
responses or late failures.

### Proposed Fix

Fail fast for required runtime steps:

- Polygon key required for market fetch/backfill;
- FRED key required for macro fetch;
- FMP key required only when using FMP importer.

### Acceptance

- Missing required key produces a clear error before network calls.

### Effort

30 minutes

---

## D22 — Provider Fetch Failures Need Typed Errors And Timeouts

**Status**: 🔴 not started
**Files**: `quantamental/data/ingest/polygon_client.py`,
`quantamental/data/ingest/fred_client.py`,
`quantamental/signals/earnings_importer.py`,
`quantamental/scripts/daily_pipeline.py`

### Problem

Some provider paths return empty DataFrames after retry exhaustion. Callers
cannot always distinguish real zero rows from API failure. Some calls may also
hang without explicit timeout support.

### Proposed Fix

1. Add typed exceptions:
   - `PolygonFetchError`;
   - `FredFetchError`;
   - provider-specific earnings errors where useful.
2. Fail pipeline steps when provider fetches truly fail.
3. Add explicit timeouts where provider clients support them.

### Acceptance

- Provider retry exhaustion marks pipeline step `FAIL`.
- Real no-data holiday/closed-market paths remain non-fatal when expected.

### Effort

1-2 hours

---

## D23 — SQLite Needs WAL And Backups

**Status**: 🔴 not started
**Files**: `quantamental/portfolio/tracker.py`,
`quantamental/signals/earnings.py`, `quantamental/scripts/daily_pipeline.py`

### Problem

SQLite stores positions, journal data, and PEAD events. There is no WAL mode
or backup rotation. A crash or disk issue can damage the local ledger.

### Proposed Fix

1. Enable `PRAGMA journal_mode=WAL`.
2. Add daily backup step:
   - copy `meta.db` to `meta.db.bak.YYYY-MM-DD`;
   - keep last 14.

### Acceptance

- Pipeline creates rotating SQLite backups.
- WAL mode is enabled in SQLite initialization.

### Effort

30-60 minutes

---

## D24 — QuestDB Write Idempotency Is Not Explicitly Tested

**Status**: 🔴 not started
**Files**: `quantamental/data/ingest/questdb_writer.py`,
`quantamental/tests/test_questdb.py`

### Problem

The project assumes repeated writes do not create damaging duplicates, but the
dedup/idempotency behavior should be explicitly tested.

### Proposed Fix

Add test that writes identical OHLCV rows twice and asserts row count is stable
or that query paths select the intended latest unique row.

### Acceptance

- Duplicate write behavior is documented by tests.

### Effort

1 hour

---

## D25 — Data-Quality Audit Ledger And Validation Manifest Are Missing

**Status**: 🟢 fixed / accepted
**Files**: `quantamental/data/quality.py`,
`quantamental/scripts/check_data.py`, `quantamental/scripts/daily_pipeline.py`,
`quantamental/alpha/reporting.py`, `quantamental/scripts/backtest_alpha.py`,
`quantamental/scripts/alpha_performance.py`, `quantamental/dashboard/data.py`,
`quantamental/dashboard/app.py`, `quantamental/dashboard/panels.py`

### Problem

Data-quality checks currently exist, but too much of the evidence is terminal
output or transient dashboard state. A stale ticker, missing OHLCV row,
provider outage, low coverage check, zero-volume row, suspicious price move, or
empty journal warning should become a persistent audit event.

Validation has the same problem: a backtest result needs a manifest explaining
exactly what data, universe, factor weights, transaction costs, code version,
and data-quality status produced it. Without that manifest, a good-looking
validation result is hard to reproduce and hard to trust later.

### Current Implementation

D25 V1 has landed:

- `quantamental/data/quality.py` persists structured data-quality events in
  SQLite.
- `scripts/check_data.py` writes failing checks to the audit ledger by default.
- `scripts/check_data.py` also records an `all_checks_passed` snapshot when
  data health is clean.
- `scripts/daily_pipeline.py` writes per-step pipeline audit events.
- `save_backtest_report()` can write a validation manifest beside report CSVs.
- `scripts/backtest_alpha.py` saves a manifest with universe, parameters,
  input row counts, input as-of dates, and git commit when available.
- `save_alpha_performance_report()` saves performance manifests and latest
  manifest pointers.
- `scripts/alpha_performance.py` saves a validation manifest and prints
  `PASS` / `WATCH` / `FAIL`.
- The dashboard Alpha tab shows latest data-quality audit events and the latest
  validation status.

### Proposed Fix

1. Add a persistent `data_quality_events` ledger in SQLite or Parquet.
2. Store one row per check with:
   - `run_id`;
   - `asof_date`;
   - `component`;
   - `symbol`;
   - `severity`;
   - `check_name`;
   - `status`;
   - `observed`;
   - `expected`;
   - `detail`;
   - `fix_hint`;
   - `created_at`.
3. Update `scripts/check_data.py` and daily pipeline checks to write audit
   rows, not just print warnings.
4. Add a validation manifest beside every backtest / alpha-performance report:
   - universe snapshot;
   - date range;
   - transaction-cost assumption;
   - factor weights;
   - code commit when available;
   - data-quality status at run time;
   - input table as-of dates.
5. Surface latest audit status and latest validation manifest on the dashboard.

### Acceptance

- Running data health creates persistent audit rows.
- Dashboard can list current failing symbols and last successful audit time.
- Every saved validation report has a manifest that can be used to rerun it.
- Alpha Validation panel shows whether the result is `PASS`, `WATCH`, or
  `FAIL`, and whether it was produced from trusted data.

### Effort

3-5 hours

---

# 🟢 Low — Hygiene And Maintainability

## D26 — Type And Unit Consistency

**Status**: 🔴 not started
**Files**: `quantamental/portfolio/tracker.py`,
`quantamental/alpha/portfolio.py`, `quantamental/dashboard/panels.py`

### Problem

Portfolio weights are percentages in some places and fractions in others.

### Proposed Fix

Adopt one internal convention, preferably fractions, and format as percentages
only at UI/print boundaries.

### Effort

1 hour

---

## D27 — Magic Numbers Should Move To Config

**Status**: 🔴 not started
**Files**: `quantamental/alpha/ranking.py`,
`quantamental/alpha/portfolio.py`, `quantamental/signals/stock.py`,
`quantamental/config/signals_registry.yaml`

### Problem

Important constants are spread across modules:

- `MAX_PRICE_AGE_DAYS = 7`;
- `PEAD_DURATION_DAYS = 28`;
- `min_names = 8`;
- `max_names = 12`;
- `max_weight = 0.15`;
- `min_weight = 0.05`;
- bucket thresholds.

### Proposed Fix

Move alpha/portfolio parameters to YAML or a config object and display current
settings in the dashboard.

### Effort

1-2 hours

---

## D28 — Macro Thresholds Are Policy, Not Pure Signal

**Status**: 🔴 not started
**Files**: `quantamental/signals/macro.py`,
`quantamental/config/settings.py`, `quantamental/config/signals_registry.yaml`

### Problem

Hard-coded macro thresholds such as VIX and yield levels embed policy
assumptions. They should be explicit and testable.

### Proposed Fix

Move thresholds into config and add historical-regime sanity tests.

### Effort

1-2 hours

---

## D29 — Trade Journal Review Reminders

**Status**: 🔴 not started
**Files**: `quantamental/portfolio/journal.py`,
`quantamental/dashboard/panels.py`

### Problem

The journal can store thesis/review data, but there is no dashboard reminder
for stale thesis reviews.

### Proposed Fix

Show positions whose thesis review is older than 30 days.

### Effort

30-60 minutes

---

# Month-2 Backtest Readiness Gate

Do not treat any backtest IC, Sharpe, hit rate, or bucket spread as decision
evidence until these are fixed:

1. D1 strict PEAD cutoff.
2. D2 walk-forward validation.
3. D3 net-of-cost validation.
4. D4 delisting/survivorship handling.
5. D8 dead-component states.
6. D25 data-quality audit ledger and validation manifest.
7. Fixed-seed reproducibility test.

Until then, backtest results are useful for debugging and upper-bound research,
not for capital confidence.

---

# Test Coverage Gaps

Add tests for:

1. End-to-end pipeline integration with fixture QuestDB/provider stubs.
2. PEAD point-in-time leakage: event on `T` excluded from `asof=T`.
3. Stale-data handling: stale names cannot benefit from stale features.
4. Dashboard global freshness gate on every tab.
5. Dead-component state: constant factor becomes `NO_VARIATION`.
6. Missing-price PnL behavior.
7. Transaction-cost monotonicity.
8. Delisting carry-through in backtest.
9. QuestDB write idempotency.
10. Backtest reproducibility.
11. Historical regime sanity around known stress periods.
12. Manual sector signal max-age behavior.
13. Data-quality ledger persistence and validation-manifest reproducibility.

---

# Suggested Implementation Order

## Phase A — Dashboard Truthfulness

1. D5 + D6: global freshness gate and as-of timestamps.
2. D8: dead-component state surfaced in validation.
3. D7: missing-price red row and no clean total PnL when incomplete.
4. D15: manual sector-signal age policy.
5. D9: stale feature neutralization.
6. D25: persistent data-quality ledger for check outputs.

## Phase B — Backtest Integrity

7. D1: strict PEAD cutoff.
8. D3: net transaction costs across validation.
9. D4: delisting/survivorship handling.
10. D2 + D25: walk-forward CLI, validation gate, and manifest.

## Phase C — Operational Hygiene

11. D18 + D13 from prior roadmap: shared market calendar and market-date state
    file naming.
12. D20: atomic universe writes.
13. D21 + D22: fail-fast secrets and typed provider errors.
14. D23: SQLite WAL and backup.
15. D17: list missing tickers in coverage checks.

## Phase D — Suggestion Quality

15. D11: conviction-weighted target weights.
16. D12: compound deployment caps.
17. D13: adaptive bucket thresholds.
18. D14: effective-weight display.

## Phase E — Cleanup

19. D24-D28 and test coverage batch.

---

# Resolved / Accepted Decisions

## R1 — 2-Day Regime Confirmation

**Status**: 🟢 fixed
**Files**: `quantamental/signals/aggregator.py`

`compute_confirmed_regime()` exists and `run_and_store()` writes both raw
`regime` and `confirmed_regime`.

Remaining follow-up: make the dashboard show raw-vs-confirmed disagreement more
prominently on all decision surfaces.

## R2 — PEAD Observe-Only

**Status**: 👀 accepted current mode
**Files**: `quantamental/alpha/ranking.py`,
`quantamental/signals/earnings.py`, `quantamental/signals/earnings_importer.py`

PEAD is collected, winsorized, displayed, and stored, but `pead_signal` has
zero alpha weight until source governance and validation pass.

## R3 — ETF / Single-Name Separation

**Status**: 🟢 fixed
**Files**: `quantamental/config/universe.py`,
`quantamental/dashboard/app.py`, `quantamental/dashboard/panels.py`

Alpha/PEAD workflows default to single-name equities. ETFs have their own
dashboard area and instrument labels.

## R4 — Query Parameter Binding

**Status**: 🟢 fixed
**Files**: `quantamental/data/ingest/questdb_connection.py`,
`quantamental/tests/test_hardening.py`

Parameterized query helpers and symbol-list binding are covered by tests.

---

# Operational Note

Last observed local state:

```text
main...origin/main [ahead 1]
```

Push when GitHub connectivity allows:

```bash
git push origin main
```
