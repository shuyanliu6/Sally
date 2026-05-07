# Month 2 Operating Handbook

> Companion to `USER_MANUAL.md`. Covers the **new** signal layer and data
> sources introduced in Month 2: sector signals (SOX/SPX, TSMC, Capex,
> API pricing), stock-level signals (EMA, RSI, volume, PEAD), and the
> 3-layer composite aggregator.
>
> This handbook is for the **operator** — what to fill in, when, where to
> find the data, and how to verify it landed.

---

## 0. Mental model

The system has **three signal layers**, combined by the aggregator:

```
   Layer            Time horizon     Update frequency    Effort
   ─────────────────────────────────────────────────────────────
   Macro            weeks–months     daily (auto)        none
   Sector           weeks            daily + monthly     low
   Stock-level      days–weeks       daily (auto)        none
                                                         ─────
   Composite        decision now     daily               just observe
```

The pipeline computes everything you don't enter manually. Your operating
burden is **3 manual data feeds**:
1. **TSMC monthly revenue** — once a month, ~10th of the month
2. **Hyperscaler Capex** — once a quarter, after Big-4 earnings
3. **AI API pricing** — weekly check, log only when something changes

Total ongoing: **~30 minutes per month**.

Dashboard reading note: the current Streamlit app is organized as
`Overview | Alpha | Signals | Portfolio | Universe`. Use `USER_MANUAL.md`
section 3 as the primary read-screen guide. This handbook remains the deeper
reference for how the Month 2 sector and stock signals are sourced, scored, and
verified.

---

## 0.1 Signal registry — customising which signals are active

All signals are declared in one file:

```
quantamental/config/signals_registry.yaml
```

Open it in any text editor. You will see three layers (`macro`, `sector`,
`stock`), each with a list of signals. Every signal has three fields:

```yaml
yield_10y:
  enabled: true    # ← flip to false to switch this signal off
  weight:  1.0     # ← relative influence within the layer (0.5 = half weight)
  description: "10Y Treasury yield MA crossover (FRED DGS10)"
```

Layer weights (how much macro vs sector vs stock influences the final
composite) are at the top of each layer block:

```yaml
macro:
  layer_weight: 1.0   # ← change this to re-balance layers
```

Changes take effect on the **next pipeline run** — no restart needed,
no Python changes needed.

### Common operations

**Disable a signal temporarily** (e.g. VIX is misbehaving):
```yaml
vix:
  enabled: false   # change true → false, save file, done
```

**Cut a signal's weight to half** (e.g. PEAD is too noisy right now):
```yaml
pead:
  weight: 0.5
```

**Give macro more influence than sector/stock**:
```yaml
macro:
  layer_weight: 1.5   # was 1.0
sector:
  layer_weight: 0.6   # was 0.8
```

**Add a brand-new signal** — four steps:
1. Write `def score_foo(df) -> int` returning `[-2, +2]` in a file
   under `signals/`
2. Add an entry to `signals_registry.yaml` under the right layer
3. Add one `if registry.is_enabled("layer", "foo")` block in the
   layer's `compute_*()` function (e.g. `compute_all_signals()` in
   `signals/macro.py`) — look at how the existing signals are wired
4. If it needs a new DB column, add it to `data/ingest/questdb_writer.py`
   `init_schema()`

### The range stays stable when you disable signals

The composite score always stays in `[-8, +8]` (macro/sector) and
`[-7, +7]` (stock), even if you only have 2 active signals. The aggregator
normalises by the sum of active weights, not a fixed count. Regime
thresholds don't need to change.

### Verify the registry is reading correctly

```bash
cd quantamental
python -c "
from signals import registry
print('Macro active :', registry.enabled_signals('macro'))
print('Sector active:', registry.enabled_signals('sector'))
print('Stock active :', registry.enabled_signals('stock'))
print('Layer weights:', registry.layer_weight('macro'),
      registry.layer_weight('sector'), registry.layer_weight('stock'))
"
```

---

## 0.2 Testing signals end-to-end

These commands let you exercise each signal layer without needing live
market data or a QuestDB connection.

### Registry smoke test

```bash
cd quantamental
python -c "
from signals import registry
assert registry.is_enabled('macro', 'vix'), 'VIX should be on by default'
assert registry.layer_weight('macro') == 1.0
assert len(registry.enabled_signals('stock')) == 4
print('Registry OK')
"
```

### Macro signals — synthetic data

```bash
python -c "
import pandas as pd, numpy as np
from signals.macro import compute_all_signals

dates = pd.date_range('2024-01-01', periods=100, freq='D')
result = compute_all_signals(
    yield_df  = pd.DataFrame({'ts': dates, 'value': np.linspace(4.5, 3.8, 100)}),
    vix_df    = pd.DataFrame({'ts': dates, 'value': [14.0]*100}),
    fed_df    = pd.DataFrame({'ts': dates, 'value': np.linspace(7000, 7500, 100)}),
    credit_df = pd.DataFrame({'ts': dates, 'value': np.linspace(120, 100, 100)}),
)
print(result)
assert -8 <= result['composite_score'] <= 8, 'composite out of range'
print('Macro signals OK')
"
```

### Sector composite — no DB needed

```bash
python -c "
from signals.sector import compute_sector_composite
c = compute_sector_composite(sox_spx_signal=2, tsmc_signal=1,
                             capex_signal=0, api_pricing_signal=-1)
assert -8 <= c <= 8
print('sector composite:', c, '  ← OK')
"
```

### Stock composite — no DB needed

```bash
python -c "
from signals.stock import stock_composite_score
c = stock_composite_score(ema_score=2, rsi_score=1,
                          volume_signal=1, pead_score=0)
assert -7 <= c <= 7
print('stock composite:', c, '  ← OK')
"
```

### 3-layer aggregator — no DB needed

```bash
python -c "
from signals.aggregator import normalize_composite, run_composite
n = normalize_composite(macro=5, sector=4, stock=3.0)
print('normalized score:', n, '  (should be in [-9, +9])')
assert -9 <= n <= 9

result = run_composite(macro_score=5, sector_score=4,
                       avg_stock_score=2.5, persist=False)
print('regime:', result['regime'])
print('action:', result['action'])
print('Aggregator OK')
"
```

### Test disabling a signal and re-enabling it

```bash
python -c "
import yaml, pathlib

path = pathlib.Path('config/signals_registry.yaml')
data = yaml.safe_load(path.read_text())

# Disable VIX
data['macro']['signals']['vix']['enabled'] = False
path.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False))

from signals import registry, macro
import importlib; importlib.reload(registry)

active = registry.enabled_signals('macro')
assert 'vix' not in active, 'VIX should be disabled'
print('With VIX off, active macro signals:', active)

# Restore
data['macro']['signals']['vix']['enabled'] = True
path.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False))
print('Registry restored — VIX back on')
"
```

> **Note**: The `yaml.dump()` call above will alphabetise keys and strip
> comments. If you want to keep the human-friendly formatting, edit the
> YAML file directly in a text editor instead of via Python.

### Run the full test suite

```bash
cd quantamental
python -m pytest tests/ -v
```

---

## 1. One-time setup

### 1.1 Fundamentals backfill (candidate list — ~30 sec)

**Scope**: fundamentals are now scoped to the **candidate list** (~26 tickers,
the stocks you'd actually trade). The full 1,386-ticker research universe is
opt-in — heavy, fragile under Yahoo's IP rate limits, and not consumed by any
current signal (PEAD reads earnings events from SQLite separately).

**Polygon free tier doesn't include fundamentals** (returns `NOT_AUTHORIZED`).
We use yfinance (Yahoo Finance) as the free fallback — no API key, ~1-2 seconds
per ticker, all the fields we need (revenue, EPS, balance sheet, cash flow).

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer/quantamental

# Default — candidate list (~26 tickers, ~30 sec)
python scripts/backfill_fundamentals.py
```

**Auto-refresh**: the daily pipeline runs `refresh_fundamentals` every Monday.
You don't need to re-run this manually unless you've just added new candidates
mid-week and want their fundamentals immediately:

```bash
# After adding a candidate via dashboard or CLI:
python scripts/backfill_fundamentals.py --tickers CRWD --no-skip-existing
```

**Full research universe (rare, opt-in)**: only useful if you're prototyping
a fundamental factor scan across all S&P 1500. Expect ~45 min and partial
failures from Yahoo throttling.

```bash
# Auto-applies a 45s pause every 100 tickers
nohup python scripts/backfill_fundamentals.py --research-universe \
  > logs/fundamentals_backfill.log 2>&1 &
tail -f logs/fundamentals_backfill.log
```

**Resume support**: re-running skips tickers with ≥4 quarters already in DB.
Safe to interrupt (Ctrl-C) and restart.

**If you have a paid Polygon plan** ($29/mo Starter or higher):

```bash
# Polygon delivers higher data quality and faster
python scripts/backfill_fundamentals.py --source polygon --income-only
```

**Verify when done**:

```bash
python -c "
from data.ingest.questdb_writer import query
df = query('SELECT count_distinct(symbol) AS unique_tickers, count() AS rows FROM fundamentals')
print(df.to_string(index=False))
"
# Default scope: ~20-26 unique tickers (candidate list minus ETFs which have no fundamentals)
# After --research-universe: ~1,000-1,400 unique tickers
```

**About data quality**: yfinance occasionally returns partial data for
small-cap tickers, recently-listed stocks, or non-US ADRs. Failures are logged
as warnings; the backfill continues. Re-running picks up failures automatically
since their row counts are < 4.

### 1.1.bis Editing the candidate list

The candidate list is the single source of truth for "what stocks I'd actually
trade." It controls fundamentals scope, PEAD signal scope, and (in Month 3+)
position sizing. Three edit paths, all kept in sync via
`config/candidate_list.json`:

**A. Dashboard (easiest)** — open `streamlit run dashboard/app.py`, scroll to
**Panel E — Candidate List Editor**, expand it, multiselect from the research
universe, add a note, click Save.

**B. CLI**:
```bash
python scripts/manage_candidates.py --show
python scripts/manage_candidates.py --add CRWD --note "cybersecurity Q2"
python scripts/manage_candidates.py --remove BABA
python scripts/manage_candidates.py --set NVDA AMD MSFT
python scripts/manage_candidates.py --reset       # back to BASE_CANDIDATES
```

**C. Direct JSON edit** — `quantamental/config/candidate_list.json`. Schema:
```json
{
  "tickers": ["AMD", "NVDA", "MSFT"],
  "updated_at": "2026-04-27T16:30:00Z",
  "notes": "Q2 rebalance"
}
```

After any edit, the next pipeline run picks up the new list automatically. The
weekly Monday `refresh_fundamentals` step will fetch fundamentals for any newly
added candidates on its next run; for immediate refresh use the `--tickers` CLI.

### 1.2 Initialize the SQLite tables for manual data

The first `log_*` command auto-creates the tables, but you can pre-init:

```bash
python -c "from signals.sector_ai_infra import init_ai_infra_db; init_ai_infra_db()"
```

This creates `tsmc_revenue`, `capex_surprise`, `api_pricing` in `data/meta.db`.

---

## 2. Signal B — TSMC Monthly Revenue

**Why it matters**: TSMC reports monthly revenue ~10 days after month-end.
This is the single best real-time proxy for AI chip demand. If TSMC revenue
is accelerating YoY, the upstream-compute thesis is alive. If it stalls, that's
a top-down concern.

### 2.1 Where to find the data

1. Go to <https://investor.tsmc.com>
2. Click "**Monthly Revenue Reports**"
3. Copy the latest "Net Revenue" number (in NT$ billions)

The press releases look like:
> *"TSMC's net revenue for January 2026 was approximately NT$293.83 billion."*

You enter `293.83` as the revenue value.

### 2.2 Logging a month

```bash
python scripts/log_tsmc_revenue.py --month 2026-04 --revenue 350.05
```

Output shows the derived YoY growth, 3-month MA YoY, and final Signal B score:

```
✅ Logged TSMC revenue for 2026-04
   Signal B (TSMC) is now: +2
```

### 2.3 First-time bulk load (recommended)

Signal B needs **13+ months** of history before YoY % growth is computed.
Backfill the last ~14 months in one sitting. Sample (verify against the IR site):

```bash
# 2025
python scripts/log_tsmc_revenue.py --month 2025-03 --revenue 285.96
python scripts/log_tsmc_revenue.py --month 2025-04 --revenue 349.59
python scripts/log_tsmc_revenue.py --month 2025-05 --revenue 320.52
python scripts/log_tsmc_revenue.py --month 2025-06 --revenue 263.71
python scripts/log_tsmc_revenue.py --month 2025-07 --revenue 323.17
python scripts/log_tsmc_revenue.py --month 2025-08 --revenue 335.77
python scripts/log_tsmc_revenue.py --month 2025-09 --revenue 330.98
python scripts/log_tsmc_revenue.py --month 2025-10 --revenue 367.39
python scripts/log_tsmc_revenue.py --month 2025-11 --revenue 276.06
python scripts/log_tsmc_revenue.py --month 2025-12 --revenue 278.16

# 2026 (so far — replace placeholders with actuals from IR site)
python scripts/log_tsmc_revenue.py --month 2026-01 --revenue 293.30
python scripts/log_tsmc_revenue.py --month 2026-02 --revenue 260.01
python scripts/log_tsmc_revenue.py --month 2026-03 --revenue 285.87
python scripts/log_tsmc_revenue.py --month 2026-04 --revenue 350.05
```

> ⚠️ The numbers above are placeholders — **always verify against TSMC's actual IR
> page** before logging. Bad data here corrupts Signal B for months.

### 2.4 Ongoing — monthly rhythm

- **First Friday of every month**: check <https://investor.tsmc.com> for the
  prior month's revenue release.
- **Log it within a day** of release.

```bash
python scripts/log_tsmc_revenue.py --month <YYYY-MM> --revenue <NT$ B>
python scripts/log_tsmc_revenue.py --show    # verify it landed and check signal
```

### 2.5 Score interpretation

| Score | Condition | Action |
|---|---|---|
| +2 | 3M MA YoY > 30% AND accelerating | Explosive demand — risk-on |
| +1 | 3M MA YoY > 15% | Healthy — positive bias |
| 0 | 0% – 15% | Normalizing — neutral |
| -1 | < 0% | Contraction — caution |
| -2 | < 0% for 2+ consecutive months | Sustained downturn — defensive |

---

## 3. Signal C — Hyperscaler Capex Surprise

**Why it matters**: META, MSFT, GOOGL, AMZN are the Big-4 AI infrastructure
buyers. When they collectively beat consensus capex by >10%, the AI compute
thesis has another quarter of fuel. When they miss, demand is softening.

### 3.1 Where to find the data

After each Big-4 earnings call (typically Tue–Thu of last week of January,
April, July, October), look for:

1. **Actual capex**: from the company's earnings press release / 10-Q.
   Often called "Capital expenditures" or "Property and equipment additions"
   on the cash flow statement.
2. **Consensus capex**: harder to find publicly. Sources:
   - Visible Alpha (paid)
   - Earnings call transcripts where analysts cite consensus
   - Bloomberg / FactSet (paid)
   - Free workaround: check the prior quarter's "guidance" — if META said "we
     expect ~$8B in Q1," and they reported $9.2B, that's a beat.

If you can't find consensus, use the company's prior **own guidance** as your
proxy consensus.

### 3.2 Logging one company's capex

```bash
python scripts/log_capex.py --quarter 2026-Q1 \
  --company META \
  --actual 8.4 \
  --consensus 7.8
```

Output:

```
✅ Logged capex for META in 2026-Q1

Signal C (Capex Surprise) for 2026-Q1: 0   ← only 1 of 4 reported, neutral
   +2: avg beat > 10%   +1: avg beat > 0%   -1: avg miss < 10%   -2: avg miss > 10%
```

Signal C activates **only when ≥2 companies** have reported for the quarter.

### 3.3 Earnings calendar (rough)

| Quarter | Reporting weeks |
|---|---|
| Q1 (Jan–Mar) | Last week of April / first week of May |
| Q2 (Apr–Jun) | Last week of July |
| Q3 (Jul–Sep) | Last week of October |
| Q4 (Oct–Dec) | Last week of January |

Each cycle = 4 entries (META, MSFT, GOOGL, AMZN).

### 3.4 View / re-score

```bash
python scripts/log_capex.py --show          # see all logged entries
python scripts/log_capex.py --score 2026-Q1 # re-compute signal for a quarter
```

---

## 4. Signal D — AI API Pricing

**Why it matters**: API pricing is a real-time signal of supply/demand
imbalance. When prices stay stable or rise, demand exceeds supply. When prices
crash >50% in a quarter (DeepSeek-style shock), it's a structural threat to the
upstream-compute thesis.

### 4.1 Where to find the data

| Provider | Pricing page |
|---|---|
| OpenAI | <https://openai.com/api/pricing> |
| Anthropic | <https://anthropic.com/pricing> (or <https://www.anthropic.com/pricing#api>) |
| Google | <https://cloud.google.com/vertex-ai/pricing> |

**What to log**: the **flagship model** for each provider, **input token price
per million**, **output price per million**.

Flagship models as of April 2026 (verify before logging):
| Provider | Model |
|---|---|
| OpenAI | gpt-4o or gpt-5 |
| Anthropic | claude-sonnet-4 (or claude-opus-4 if you prefer the top-tier) |
| Google | gemini-2.5-pro |

### 4.2 Logging today's snapshot

```bash
TODAY=$(date +%F)

python scripts/log_api_pricing.py --date $TODAY --provider OpenAI \
  --model gpt-4o --in 2.50 --out 10.00

python scripts/log_api_pricing.py --date $TODAY --provider Anthropic \
  --model claude-sonnet-4 --in 3.00 --out 15.00

python scripts/log_api_pricing.py --date $TODAY --provider Google \
  --model gemini-2.5-pro --in 1.25 --out 10.00

python scripts/log_api_pricing.py --score
```

### 4.3 Cadence

- **Weekly check** (Monday morning, 5 min): visit each pricing page.
- **Log only when something changes** — duplicate same-price entries are
  harmless but clutter the DB.
- **Always log immediately** after a major model launch or pricing change. If
  Anthropic releases a 50%-cheaper model, log it the same day.

### 4.4 Score interpretation

| Score | Condition |
|---|---|
| +1 | Prices rising or stable (no cuts in 90 days) |
| 0 | Normal efficiency gains (drop < 30% per quarter) |
| -1 | Drop 30–50% per quarter |
| -2 | Drop > 50% per quarter — DeepSeek-scale shock |

---

## 5. Verification — checking signals are working

After logging some data, verify the signals are computing correctly:

### 5.1 Run sector signals manually

```bash
python -c "
from signals.sector import run_sector_signals
result = run_sector_signals(persist=False)
print(result)
"
```

Expected output (something like):

```
Sector signals: A(SOX/SPX)=+1 B(TSMC)=+2 C(Capex)=0 D(API)=+1 → composite=+4
```

### 5.2 Run the full composite (macro + sector + stock)

After Stock signals are computed for the day, the aggregator can produce a final
score:

```bash
python -c "
from signals.aggregator import run_composite
# Plug in current values from the dashboard or DB
result = run_composite(macro_score=5, sector_score=4, avg_stock_score=2.5, persist=False)
print(result)
"
```

Output:

```
{
  'regime': 'BUY',
  'normalized_score': 5.6,
  'action': 'Proceed with scheduled entries',
  ...
}
```

### 5.3 Check the SQLite tables directly

```bash
sqlite3 data/meta.db "SELECT * FROM tsmc_revenue ORDER BY month DESC LIMIT 5;"
sqlite3 data/meta.db "SELECT * FROM api_pricing ORDER BY date DESC LIMIT 10;"
sqlite3 data/meta.db "SELECT * FROM capex_surprise ORDER BY quarter DESC, company;"
```

### 5.4 Run the data health check

```bash
python scripts/check_data.py --days 60
```

The macro/OHLCV sections work as before. New signal tables will be reported
when the dashboard v0.2 is wired up (Week 8 deliverable).

---

## 6. Daily / weekly / monthly rhythm

Recommended cadence once everything is in place:

### Daily (~2 minutes)
- Pipeline runs at 5 PM ET via cron — no human action.
- **Glance at the dashboard** the next morning to see today's regime + composite score.

### Weekly (~10 minutes, Monday morning)
- Check OpenAI / Anthropic / Google pricing pages
- Log any price changes:
  ```bash
  python scripts/log_api_pricing.py --date $(date +%F) --provider <X> --model <Y> --in <Z> --out <W>
  ```
- `python scripts/log_api_pricing.py --score` to confirm Signal D

### Monthly (~15 minutes, ~5th–10th of month)
- Visit <https://investor.tsmc.com>
- Log the prior month's revenue:
  ```bash
  python scripts/log_tsmc_revenue.py --month YYYY-MM --revenue <NT$ B>
  ```
- `python scripts/log_tsmc_revenue.py --show` to confirm Signal B updated

### Quarterly (~30 minutes, Big-4 earnings weeks)
- META, MSFT, GOOGL, AMZN report Q1/Q2/Q3/Q4 results
- For each company:
  ```bash
  python scripts/log_capex.py --quarter YYYY-Qn --company XXX \
    --actual <$B> --consensus <$B>
  ```
- `python scripts/log_capex.py --score YYYY-Qn` after all 4 are in

---

## 7. Quick reference card

```bash
# ── Setup (once) ──────────────────────────────────────────────────────────
python scripts/backfill_fundamentals.py                # candidate list, ~30s
python -c "from signals.sector_ai_infra import init_ai_infra_db; init_ai_infra_db()"

# ── Manual signals ────────────────────────────────────────────────────────
# TSMC (monthly)
python scripts/log_tsmc_revenue.py --month YYYY-MM --revenue <NT$B>
python scripts/log_tsmc_revenue.py --show

# Capex (quarterly)
python scripts/log_capex.py --quarter YYYY-Qn --company XXX \
                            --actual <$B> --consensus <$B>
python scripts/log_capex.py --score YYYY-Qn

# API pricing (weekly)
python scripts/log_api_pricing.py --date YYYY-MM-DD --provider X \
                                  --model Y --in <$/M> --out <$/M>
python scripts/log_api_pricing.py --score

# ── Verification ──────────────────────────────────────────────────────────
python -c "from signals.sector import run_sector_signals; print(run_sector_signals(persist=False))"
python scripts/check_data.py --days 60
```

---

## 8. Troubleshooting

### "Signal B is 0 even though I logged data"
- Need ≥13 months of history for YoY computation.
- Check: `python scripts/log_tsmc_revenue.py --show` — does the latest row have
  a non-null `YoY %` and `3M MA YoY`? If not, you're missing the prior-year
  baseline month.

### "Signal C is 0 right after I logged META"
- Signal C requires **≥2 companies reporting** for the same quarter.
- After all 4 (or at least 2) report, run:
  ```bash
  python scripts/log_capex.py --score YYYY-Qn
  ```

### "Signal D is +1 when I expected 0"
- Default behavior with only 1 observation (or all-stable observations) is +1.
- Log a price from 90+ days ago to establish a baseline:
  ```bash
  python scripts/log_api_pricing.py --date 2026-01-26 --provider OpenAI \
    --model gpt-4o --in 5.00 --out 15.00
  ```
- Then today's price will be compared against that baseline.

### "Fundamentals backfill: NOT_AUTHORIZED from Polygon"
- This means you tried `--source polygon` on the free tier — Polygon's
  `/v3/reference/financials` is paid-only.
- **Fix**: drop the `--source polygon` flag (default is yfinance, free).

### "Fundamentals backfill is taking too long"
- Default scope is now the candidate list (~30 sec). If yours runs longer,
  you may have invoked `--research-universe` (intentional ~45 min run).
- Resume support: kill (Ctrl-C) and re-run — skips tickers with ≥4 quarters.
- For research-universe runs, Yahoo's IP rate limiter kicks in. The CLI
  auto-applies a 45s pause every 100 tickers when `--research-universe` is set.
  Override with `--batch-pause 60` if you still see scattered empty responses.

### "yfinance returns NaN or empty for some tickers"
- Expected for 5–10% of tickers (recently-listed, small-cap ADRs, delisted).
- Yahoo's coverage is good for S&P 500, weaker for S&P 600.
- Re-run later — Yahoo updates data over time.
- If a key ticker (in your candidate list) is missing, fetch manually:
  ```bash
  python -c "from data.ingest.yfinance_fundamentals import fetch_fundamentals_yf; print(fetch_fundamentals_yf('NVDA'))"
  ```

---

## 9. What's still being built (Month 2 remaining work)

| Deliverable | Status |
|---|---|
| Sector signals (A + AI-infra B/C/D) | ✅ done |
| Stock signals (EMA, RSI, Volume, PEAD) | ✅ done |
| 3-layer composite aggregator | ✅ done |
| Fundamentals ingestion module | ✅ done |
| Daily pipeline integration | 🔨 in progress |
| **Backtest engine (vectorbt)** | 🔨 next |
| **Dashboard v0.2 (panels E/F/G)** | pending |
| Validation + parameter rationale doc | pending |

When pipeline integration ships, the daily pipeline (`python scripts/daily_pipeline.py
--step all`) will automatically compute sector + stock + composite signals
alongside the existing macro flow.

---

*This handbook is paired with `USER_MANUAL.md` (Month 1 setup + daily ops) and
`TECH_DEBT.md` (deferred items D4–D13).*
