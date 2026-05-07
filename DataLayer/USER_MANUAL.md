# Quantamental System — Month 1 User Manual

> **Operating manual for the daily workflow.** Covers setup, daily operations, position management, dashboard reading, and troubleshooting. Read top-to-bottom on first use; bookmark sections for daily reference.

---

## 1. First-time setup (do once)

### 1.1 Start QuestDB

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer
docker compose up -d
```

Verify it's running: open <http://localhost:9000> in your browser. You should see the QuestDB Web Console.

### 1.2 Install Python dependencies

```bash
pip install -r requirements.txt
```

### 1.3 Configure API keys

Hidden file at `quantamental/config/.env` (already exists). Open it and fill in:

```bash
open -e quantamental/config/.env
```

| Variable | Where to get it |
|---|---|
| `POLYGON_API_KEY` | <https://polygon.io/dashboard> → API Keys |
| `FRED_API_KEY` | <https://fred.stlouisfed.org/docs/api/api_key.html> |
| `TELEGRAM_BOT_TOKEN` | Optional — `@BotFather` on Telegram |
| `TELEGRAM_CHAT_ID` | Optional — `@userinfobot` on Telegram |

### 1.4 Build the research universe (one-time, ~3 sec)

The system uses two ticker concepts: a **research universe** (~1,200 filtered S&P 1500 stocks — what gets stored in QuestDB for analysis) and a **candidate list** (subset you actively trade — starts as the 26 seed tickers, fully editable).

```bash
cd quantamental
python scripts/build_universe.py --stage static
```
- Scrapes S&P 500 + 400 + 600 from Wikipedia
- Drops REITs (GICS sector=Real Estate), ADRs (Polygon type=ADRC), SPACs/Trust/Preferred (name patterns)
- Writes `config/research_tickers.json` with ~1,300 tickers

### 1.5 Migrate the daily_ohlcv schema (one-time, destructive)

The original schema was sized for 27 tickers (`SYMBOL CAPACITY 64`). For ~1,200 tickers we need 2048. QuestDB can't ALTER capacity in place — drop and recreate:

```bash
python scripts/build_universe.py --apply-schema-migration
```
Confirms with a `[y/N]` prompt. Any existing OHLCV data is wiped — backfill follows next.

### 1.6 Backfill ~10 months of history (one-time, ~50–100 min)

```bash
python scripts/backfill.py
```
- Default start: **2024-06-01** (~10 months — sufficient for 60-day MA + buffer)
- Auto-picks **per-date strategy** for wide universes (1 grouped call per trading day)
- ~250 trading days × 12s rate limit ≈ 50 minutes on free Polygon tier
- Resumable — re-running skips dates already in DB
- Override start: `python scripts/backfill.py --start 2024-01-01`
- Backfill only 27 candidates (much faster, < 5 min): `python scripts/backfill.py --candidates-only`

### 1.7 Refine the research universe (after backfill completes)

```bash
python scripts/build_universe.py --stage refine
```
- Drops tickers with latest close < $5 (penny stocks)
- Drops tickers with trailing 30-day ADDV < $2M (illiquid)
- Drops recent IPOs (< 252 days of history)
- Final research universe: ~1,200 tickers

### 1.8 Verify everything is healthy

```bash
python scripts/check_data.py
```

Expected: green checkmarks for OHLCV coverage and macro indicators. Empty journal/portfolio is normal (you haven't traded yet).

---

## 2. Daily operations

### 2.1 The one command you run every day

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer/quantamental
python scripts/daily_pipeline.py --step all
```

If you installed the project with `pip install -e ".[dev]"`, the package-style
equivalent is:

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer
quantamental-pipeline --step all
```

This runs **5 sequential steps** (after market close at 4 PM ET, ideally 5 PM ET to allow Polygon settlement):

| Step               | What it does                                                           | Time   |
| ------------------ | ---------------------------------------------------------------------- | ------ |
| `fetch_market`     | Pull OHLCV for the entire research universe (1 grouped API call)       | ~3 sec |
| `fetch_macro`      | Pull recent FRED data, dedup against existing rows                     | ~5 sec |
| `calc_signals`     | Score yield/VIX/Fed/credit, classify regime, write to `regime_signals` | ~2 sec |
| `update_portfolio` | Recalc P&L for open positions using latest prices                      | ~1 sec |
| `check_stops`      | Alert if any position is within 5% of its stop-loss                    | ~1 sec |

Total: **~10–15 seconds** end-to-end.

### 2.2 If something fails partway through

The pipeline saves state to `quantamental/logs/pipeline_state_YYYY-MM-DD.json` after each step. **Re-running skips already-completed steps**:

```bash
# Resume — skip completed steps, retry failed ones
python scripts/daily_pipeline.py --resume

# Or equivalently
python scripts/daily_pipeline.py --step all
```

If you want to start over (rare):
```bash
python scripts/daily_pipeline.py --step all --force
```

### 2.3 Run a single step

Useful for debugging or partial reruns:

```bash
python scripts/daily_pipeline.py --step fetch_market
python scripts/daily_pipeline.py --step calc_signals
python scripts/daily_pipeline.py --step check_stops
```

Single-step runs **always execute** regardless of state file (manual override).

### 2.4 Set up automated cron

Add this line to your crontab (runs at 5:00 PM ET = 21:00 UTC, weekdays only):

```bash
crontab -e
```

```cron
0 21 * * 1-5 cd /Users/shuyan/Desktop/nothing/Sally/DataLayer/quantamental && python scripts/daily_pipeline.py --step all >> logs/pipeline.log 2>&1
```

Verify it's installed:
```bash
crontab -l
```

### 2.5 Open the dashboard

```bash
streamlit run dashboard/app.py
```

Browser opens automatically at <http://localhost:8501>. Auto-refreshes every 60 seconds without freezing the UI.

### 2.6 Check data health anytime

```bash
python scripts/check_data.py            # last 20 trading days
python scripts/check_data.py --days 60  # deeper look
```

Package-style equivalent:

```bash
quantamental-check-data --days 60
```

Exit code `0` = healthy, `1` = issues found.

---

## 3. Reading the dashboard

The dashboard is now a tabbed decision cockpit:

```text
Overview | Alpha | Signals | Portfolio | Universe
```

It is designed for **decision support**, not automatic trading. The right way
to read it is top-down:

1. **Can I take risk?** Start with `Overview` and `Signals`.
2. **What should I research or buy?** Move to `Alpha`.
3. **Does the model deserve trust?** Read `Alpha Validation` before acting.
4. **What does my current book require?** Check `Portfolio`.
5. **Is the candidate universe correct?** Use `Universe` only when the watchlist
   itself needs editing.

### 3.1 Overview — command center

The `Overview` tab compresses the whole system into a daily decision queue.

Top metrics:

| Metric | Meaning | How to read it |
|---|---|---|
| `Stance` | System-level posture: Deploy, Hold, or De-risk | This is the first risk filter before any stock idea |
| `Sector` | Latest AI-infra sector composite | Positive supports deployment; negative means cap exposure |
| `Top Alpha` | Highest ranked ticker today | Research lead, not an order |
| `Target Risk` | Sum of current model target weights | Low value means the model wants cash |
| `Open Positions` | Number of active positions in SQLite | Use with the rebalance queue |

Action cards:

- `Deploy selectively`: macro and sector context allow new long exposure.
- `Selective only`: sector is weak, so require stronger stock evidence and
  smaller deployment.
- `De-risk`: macro regime blocks new buys; cash and stop review come first.
- Ticker-specific cards such as `Research NVDA`, `Add MSFT`, or `Review AMD`
  compare current holdings with alpha target weights.

Risk flags:

- `Macro permits risk`: macro composite is supportive.
- `Macro below neutral`: risk budget should be smaller.
- `Macro risk-off: block new buys`: do not open new long positions under V1.
- `Sector cap active`: even good stock ranks should be position-sized smaller.
- `Target exposure only X%`: the portfolio engine is intentionally holding cash.

Principle: `Overview` is not trying to explain every signal. It answers the
manager question: **what deserves attention today?**

### 3.2 Alpha — stock selection and validation

The `Alpha` tab has two parts: `Alpha Book` and `Alpha Validation`.

#### Alpha Book

`Alpha Book` is the live ranker output for the AI-infra candidate universe.

Important columns:

| Column | Meaning |
|---|---|
| `Rank` | Cross-sectional order, best idea first |
| `Ticker` | Candidate symbol |
| `Bucket` | `TOP_BUY`, `BUY`, `HOLD`, or `AVOID` |
| `Alpha` | Transparent weighted score, not ML |
| `Target` | Suggested portfolio weight after risk rules |
| `Stock` | Stock-level composite from EMA, RSI, volume, PEAD |
| `Momentum` | Recent return/momentum input |
| `Vol` | Recent realized volatility, used as risk control |
| `Drawdown` | Recent peak-to-trough pressure |
| `Macro` | Macro regime attached to the rank row |
| `Sector` | Sector composite attached to the rank row |

Bucket interpretation:

| Bucket | Meaning | Operating action |
|---|---|---|
| `TOP_BUY` | Highest conviction names after scoring | Research first; eligible for target weights if risk allows |
| `BUY` | Positive rank, but lower conviction than top names | Candidate for weekly rebalance |
| `HOLD` | Not bad enough to avoid, not strong enough to add | Keep watching; avoid forcing action |
| `AVOID` | Weak relative score or risk profile | Do not initiate unless you have an external thesis |

Portfolio rules embedded in targets:

- Long-only V1.
- Weekly rebalance bias, not daily trading.
- Max single-stock weight: 15%.
- Minimum active position weight: 5%.
- `RISK_OFF` blocks new buys and pushes cash to at least 50%.
- Negative sector composite caps deployed exposure.

Principle: the alpha score is **cross-sectional**. A ticker is ranked against
the current candidate universe, not judged in isolation.

#### Alpha Validation

This is the fund-manager quality-control panel. It asks whether the ranker has
actually shown selection power historically.

Top metrics:

| Metric | Meaning | Good sign |
|---|---|---|
| `20D top spread` | Forward 20-trading-day excess return of `TOP_BUY/BUY` minus `AVOID` | Positive |
| `40D top spread` | Same idea over 40 trading days | Positive |
| `20D rank IC` | Correlation between rank score and 20D forward excess return | Positive and persistent |
| `40D rank IC` | Correlation between rank score and 40D forward excess return | Positive and persistent |

Bucket table:

| Column | Meaning |
|---|---|
| `Avg excess vs SMH` | Bucket average return minus `SMH` over the same horizon |
| `Avg excess vs EW` | Bucket average return minus equal-weight candidate basket |
| `Win vs SMH` | Share of observations beating `SMH` |
| `Win vs EW` | Share of observations beating the candidate basket |
| `Avg alpha` | Average alpha score inside the bucket |

How to interpret:

- If `TOP_BUY/BUY` beats `AVOID`, the ranker is separating stronger names from
  weaker names.
- If `top spread` is negative, the ranker is not yet proven, even if today's
  top names look intuitively attractive.
- If `Rank IC` is positive but spread is negative, ordering has some signal but
  bucket thresholds or portfolio construction may need work.
- If both spread and IC are weak, treat the engine as a monitoring tool until
  the signal design improves.

Principle: this panel prevents the dashboard from becoming a storytelling
machine. A good-looking rank must be backed by forward-return evidence.

To refresh this panel:

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer/quantamental
python scripts/alpha_performance.py --start 2025-01-01 --end 2026-04-01
```

### 3.3 Signals — why the risk posture changed

The `Signals` tab explains the inputs behind the stance.

#### Macro Regime

The macro regime combines four signals:

| Signal | Source | Intuition |
|---|---|---|
| 10Y yield | FRED `DGS10` | Falling yields support equities; rising yields tighten conditions |
| VIX | Market volatility | Low/normal volatility supports risk; panic raises caution |
| Fed balance sheet | FRED `WALCL` | Expansion supports liquidity; contraction tightens liquidity |
| Credit spread | FRED IG OAS | Tightening spreads show risk appetite; widening spreads warn stress |

Regime interpretation:

| Regime | Composite | Action |
|---|---|---|
| `RISK_ON` | +5 to +8 | Full allocation per plan; accelerate batch entries |
| `MODERATE_ON` | +2 to +4 | Maintain positions; proceed with scheduled entries |
| `NEUTRAL` | -1 to +1 | Hold; no new entries unless event-driven |
| `MODERATE_OFF` | -4 to -2 | Pause new entries; tighten stop-losses by 5% |
| `RISK_OFF` | -8 to -5 | Reduce exposure; block new long allocations in V1 |

#### Sector Signal

The sector panel measures whether the AI-infra cycle is supportive.

Core inputs:

- `SMH/SPY` or semiconductor relative trend.
- TSMC monthly revenue.
- Hyperscaler capex surprise.
- AI API pricing pressure.

Read it as a deployment throttle:

- Positive sector score: alpha ranks can be used normally if macro allows.
- Slightly negative sector score: only strongest names deserve capital.
- Deeply negative sector score: cap exposure and avoid expanding the book.

#### Stock Signal Detail

The stock detail panel explains ticker-level evidence:

- EMA trend: medium-term trend quality.
- RSI: overbought/oversold pressure.
- Volume: confirmation or distribution.
- PEAD: post-earnings announcement drift.
- Stock composite: normalized aggregate of these signals.

Principle: a strong stock score can identify relative winners, but it should not
override macro `RISK_OFF` or a broken sector backdrop.

#### Signal History

The history chart shows whether regime changes are stable or noisy.

Use it to ask:

- Did the composite just flip today, or has it been trending for weeks?
- Is the system improving from risk-off to neutral, or deteriorating from
  risk-on to moderate-off?
- Are current recommendations aligned with the recent regime path?

### 3.4 Portfolio — current book risk

The `Portfolio` tab compares the model target book with actual holdings.

Read in this order:

1. Target vs current weight: find adds, trims, exits, and unowned buy
   candidates.
2. P&L table: check whether losses are thesis breaks or normal volatility.
3. Stop-loss monitor: review names near stop before considering new buys.

Common actions:

| Dashboard action | Meaning |
|---|---|
| `NEW BUY` | Model has target weight, but you do not own it |
| `ADD` | Current weight is below target |
| `TRIM` | Current weight is above target |
| `EXIT/REVIEW` | Position no longer has target allocation |
| `HOLD` | Current weight is close enough to target |
| `WATCH` | No immediate portfolio action |

Principle: portfolio management comes after signal review. Do not add a new
name while an existing position is breaching risk limits.

### 3.5 Universe — candidate list control

The `Universe` tab edits the candidate list. This is the active stock universe
for alpha ranking, dashboard display, fundamentals refresh, and candidate-level
stock signals.

Use it when:

- A new AI-infra name deserves ongoing monitoring.
- A ticker thesis is stale and should be removed.
- You want to group candidates by sector or theme.
- You need to leave a note explaining why the list changed.

Principle: garbage in, garbage out. The ranker can only select from the names
you allow into the candidate universe.

### 3.6 Daily dashboard reading checklist

Before making any trade decision:

1. Confirm data is fresh with `python scripts/check_data.py`.
2. Open `Overview`; note stance, target exposure, and risk flags.
3. Open `Alpha`; check whether validation supports trusting the ranker.
4. Review top `TOP_BUY` and `BUY` names, but reject them if macro or sector
   gates are closed.
5. Open `Portfolio`; handle stop-loss and drift issues before new entries.
6. Log any actual trade decision in the journal.

---

## 4. Managing positions

### 4.1 Add a position

```python
cd quantamental
python -c "
from portfolio.tracker import init_db, add_position
init_db()
add_position(
    symbol='NVDA',
    entry_date='2026-04-25',
    entry_price=208.27,
    shares=10,
    target_weight=0.05,        # optional, fraction (0.05 = 5%)
    stop_loss_price=185.00,    # optional but recommended
    thesis='AI compute cycle, upstream beneficiary'
)
"
```

### 4.2 Log a trade decision (do this every time you trade)

```python
python -c "
from portfolio.journal import log_trade
log_trade(
    symbol='NVDA',
    action='BUY',                          # BUY | SELL | ADD | TRIM
    quantity=10,
    price=208.27,
    trigger_reason='Regime RISK_ON, score +5',
    emotion='Confident',                    # honest single word
    thesis_still_valid='YES',               # YES | NO | NEEDS_REVIEW
    notes='Batch 1 of 3 planned entries'
)
"
```

### 4.3 Close a position

```python
python -c "
from portfolio.tracker import close_position, get_open_positions
print(get_open_positions())                # find the id
close_position(pos_id=1)                   # use the id from above
"
```

### 4.4 Fill in 30-day review

Spec §4.4: trade journal has a `review_30d` field — fill it in 30 days after the entry to track decision quality.

```python
python -c "
from portfolio.journal import fill_30d_review
fill_30d_review(entry_id=1, review_text='Thesis still intact, position +12%, holding')
"
```

### 4.5 List recent trades

```python
python -c "
from portfolio.journal import get_recent
print(get_recent(n=10).to_string())
"
```

---

## 5. Managing the research universe & candidate list

The system has two ticker concepts. Understanding the difference is core to the workflow.

### 5.1 Two concepts

| | Research universe | Candidate list |
|---|---|---|
| **Purpose** | What's stored in QuestDB for analysis | What you actively consider for trading |
| **Source** | `config/research_tickers.json` (auto-generated) | `config/candidate_list.json` (user-curated) |
| **Used by** | `fetch_market` step, `backfill.py` | Signals, portfolio, dashboard |
| **Size** | ~1,200 tickers | Whatever you set (default: 26 seeds) |
| **Update frequency** | Quarterly (re-run `build_universe.py`) | Anytime, after research |

**Default fallback chain**: if `candidate_list.json` doesn't exist → use BASE_CANDIDATES (the 26 seeds in `config/universe.py`). If `research_tickers.json` doesn't exist → fall back to candidate list.

### 5.2 Refresh the research universe (quarterly)

```bash
python scripts/build_universe.py --stage all
```
This runs static (Wikipedia scrape + Polygon types) AND refine (liquidity filters) in one go. Recommended every quarter to catch index changes.

### 5.3 See your current candidate list

```bash
python scripts/manage_candidates.py --show
```
Shows the active list with its source (BASE_CANDIDATES vs JSON) and any note from the last update.

### 5.4 Add a candidate after research

```bash
python scripts/manage_candidates.py --add CRWD --note "Cybersecurity exposure, Q2 thesis"
```
- First add creates `config/candidate_list.json` (gitignored)
- Tickers normalized to uppercase
- Duplicates auto-filtered

Multiple at once:
```bash
python scripts/manage_candidates.py --add CRWD PANW ZS --note "Security suite expansion"
```

### 5.5 Drop a candidate

```bash
python scripts/manage_candidates.py --remove BABA --note "China regulatory overhang"
```

### 5.6 Replace the entire list

After major thesis revision:
```bash
python scripts/manage_candidates.py --set NVDA TSM AVGO MSFT GOOGL META --note "Q3 narrowed to AI compute pure plays"
```

### 5.7 Reset to defaults

```bash
python scripts/manage_candidates.py --reset
```
Deletes `candidate_list.json` and falls back to BASE_CANDIDATES (the 26 seeds).

### 5.8 Researching a stock that's NOT in your candidate list

Any ticker in the research universe is queryable via QuestDB:

```sql
-- In the QuestDB Web Console at http://localhost:9000
SELECT * FROM daily_ohlcv
WHERE symbol = 'CRWD'
ORDER BY ts DESC
LIMIT 60;
```

Or in Python:
```python
from data.ingest.questdb_writer import query
df = query("SELECT * FROM daily_ohlcv WHERE symbol = 'CRWD' ORDER BY ts DESC LIMIT 60")
```

If you decide to trade it: `python scripts/manage_candidates.py --add CRWD`.

If a ticker isn't in the research universe (e.g., not in S&P 1500, or got filtered out by liquidity), expand the universe by adjusting filters:
```bash
python scripts/build_universe.py --stage refine --addv-min 1000000  # lower ADDV threshold
```

---

## 6. The macro signal model — what each indicator means

(Spec §6 — for reference when reviewing dashboard.)

### 6.1 10Y Treasury Yield (DGS10)
- **Bullish**: 20-day MA below 60-day MA (yields falling = stocks attractive)
- **Strong bullish**: above + yield < 4.0%
- **Bearish**: 20MA above 60MA (yields rising = bond competition)
- **Strong bearish**: above + yield > 5.0%

### 6.2 VIX (Volatility Index)
- < 15 → +2 (extreme complacency, trends continue)
- 15–20 → +1 (normal low vol)
- 20–25 → 0 (neutral)
- 25–35 → −1 (high vol, reduce exposure)
- ≥ 35 → −2 (panic; **manual contrarian override possible** — see Note below)

> **Note on contrarian VIX**: spec §6.2 says VIX > 35 with intact fundamentals can be a contrarian buy. The system reports −2 by default; you must manually verify thesis still valid before treating it as +2.

### 6.3 Fed Balance Sheet (WALCL)
- 13-week MA of week-over-week change
- Positive = expansion = bullish
- Negative = contraction (QT) = bearish
- Updates **weekly on Thursdays**

### 6.4 Credit Spread (BAMLC0A0CM, IG OAS)
- 20-day vs 60-day MA crossover
- Tightening → +1 (risk appetite up)
- Widening → −1
- Widening + absolute > 200 bps → −2 (recession watch)
- **Lead indicator**: typically 2–4 weeks ahead of equities

---

## 7. Troubleshooting

### Pipeline says `fetch_market: FAIL`
1. Check QuestDB is running: `docker ps | grep questdb`
2. Check Polygon key is valid: `python -c "from config.settings import POLYGON_API_KEY; print(POLYGON_API_KEY[:6])"` (should print first 6 chars, not empty)
3. Check today is a trading day — system auto-skips weekends/holidays so a Saturday run will just fetch Friday's data
4. Re-run with `--resume` to retry only the failed step

### Polygon `429 Too Many Requests`
- Free tier = 5 req/min. The pipeline auto-rate-limits to this.
- If you see 429 anyway, the system waits 65 seconds and retries automatically (see logs)
- To upgrade: Polygon Starter $29/mo, then set in `.env`: `POLYGON_REQUESTS_PER_MINUTE=300`

### Polygon `NOT_AUTHORIZED Your plan doesn't include this data timeframe`
- You're querying same-day data that the free plan doesn't cover
- The system always queries `prev_trading_day()` — if you're seeing this, your local clock may be wrong, or it's a holiday the calendar doesn't know about

### Dashboard shows "QuestDB unavailable"
```bash
docker compose up -d         # start it
docker logs questdb          # check for errors
docker compose restart questdb  # nuclear option
```

### "FRED data missing" warnings
- FRED has occasional outages (rare). The pipeline caches the last known value.
- If persistent, check FRED status: <https://fred.stlouisfed.org/>

### Tests failing
```bash
cd quantamental
python -m pytest tests/ -v                              # run all
python -m pytest tests/ --ignore=tests/test_questdb.py  # skip DB tests if Docker down
python -m pytest tests/test_questdb.py -v               # only DB tests
```

### See what's in the database
- Web UI: <http://localhost:9000> (QuestDB console — paste any SELECT)
- CLI sample queries:
  ```sql
  SELECT count(*) FROM daily_ohlcv;
  SELECT symbol, count() FROM daily_ohlcv GROUP BY symbol ORDER BY symbol;
  SELECT * FROM regime_signals ORDER BY ts DESC LIMIT 10;
  SELECT * FROM macro_indicators WHERE indicator = 'yield_10y' ORDER BY ts DESC LIMIT 5;
  ```

### View pipeline logs
```bash
tail -f logs/pipeline.log              # live tail
cat logs/pipeline_state_$(date +%F).json  # today's run state
```

---

## 8. Month 1 success criteria checklist

(Spec §11 — Day 30 review.)

- [ ] QuestDB has 20+ trading days of OHLCV for all 27 tickers
- [ ] Macro signal engine produces a daily regime classification
- [ ] Streamlit dashboard renders all four panels with live data
- [ ] At least one real position entered, logged in trade journal
- [ ] Daily pipeline runs automatically via cron for 5+ consecutive trading days
- [ ] Trade journal contains 3+ entries with all required fields filled

---

## 9. Quick reference card

```bash
# Setup (once)
docker compose up -d
pip install -r requirements.txt
python scripts/build_universe.py --stage static          # generate research universe
python scripts/build_universe.py --apply-schema-migration  # bump SYMBOL CAPACITY
python scripts/backfill.py                                 # default start 2024-06-01
python scripts/build_universe.py --stage refine           # liquidity filters

# Daily
python scripts/daily_pipeline.py --step all       # the one command
python scripts/check_data.py                       # health check
streamlit run dashboard/app.py                     # dashboard

# Recovery
python scripts/daily_pipeline.py --resume                  # skip completed
python scripts/daily_pipeline.py --step all --force        # full restart

# Per-step
python scripts/daily_pipeline.py --step fetch_market
python scripts/daily_pipeline.py --step fetch_macro
python scripts/daily_pipeline.py --step calc_signals
python scripts/daily_pipeline.py --step update_portfolio
python scripts/daily_pipeline.py --step check_stops

# Candidate list management
python scripts/manage_candidates.py --show
python scripts/manage_candidates.py --add CRWD --note "Why I added it"
python scripts/manage_candidates.py --remove BABA --note "Why I removed it"
python scripts/manage_candidates.py --set NVDA TSM AVGO --note "Q3 narrowed thesis"
python scripts/manage_candidates.py --reset                # back to BASE 26

# Universe maintenance (quarterly)
python scripts/build_universe.py --stage all              # static + refine

# Tests
python -m pytest tests/ -v
```

---

## 10. What's deferred to Month 2

(Spec §11 — explicitly out of scope for Month 1.)

- Sector-level signals (per-sub-universe scoring)
- Stock-level signals (momentum, relative strength)
- Backtesting engine
- vectorbt integration
- Signal aggregation beyond macro layer
- Tech debt items D4–D11 (see `TECH_DEBT.md`)

When ready, just ask: "let's start Month 2."
