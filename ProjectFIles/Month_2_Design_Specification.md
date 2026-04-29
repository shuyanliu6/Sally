# Month 2 Design Specification

## Signal System + Backtesting Engine

**Quantamental AI Infra Investment System**
**Version 1.0 | May 2026**

---

## Table of Contents

1. [Month 2 Overview](#1-month-2-overview)
2. [Prerequisites & Dependencies](#2-prerequisites--dependencies)
3. [Sector Timing Signal Design](#3-sector-timing-signal-design)
4. [Stock-Level Signal Design](#4-stock-level-signal-design)
5. [Signal Aggregation Engine](#5-signal-aggregation-engine)
6. [Backtesting Engine Design](#6-backtesting-engine-design)
7. [Historical Data Management](#7-historical-data-management)
8. [QuestDB Schema Extensions](#8-questdb-schema-extensions)
9. [Dashboard Upgrade (v0.2)](#9-dashboard-upgrade-v02)
10. [Automation Updates](#10-automation-updates)
11. [Testing & Validation](#11-testing--validation)
12. [Risks & Contingency](#12-risks--contingency)
13. [Success Criteria](#13-success-criteria)

---

## 1. Month 2 Overview

### 1.1 Purpose

Month 2 builds the signal generation and backtesting layer on top of Month 1's data infrastructure. By the end of this month, the system should produce a daily composite signal score across macro, sector, and stock levels, and you should have a backtesting engine capable of validating any signal or strategy against historical data.

### 1.2 Inputs from Month 1

The following Month 1 deliverables are assumed to be operational:

| Component | Status Required |
|-----------|----------------|
| QuestDB (Docker) | Running, receiving daily data |
| Polygon.io data pipeline | Daily OHLCV for ~27 tickers |
| FRED macro pipeline | 4 macro indicators updating daily |
| Macro signal engine | Producing daily regime classification |
| Portfolio tracker + journal | Functional in SQLite |
| Streamlit dashboard v0.1 | Rendering 4 panels |

If any of the above are not complete, prioritize finishing Month 1 deliverables before starting Month 2. Do not build on an unstable foundation.

### 1.3 Deliverables

| Deliverable | Description | Priority |
|-------------|-------------|----------|
| Sector Signal Module | SOX/SPX relative strength, TSMC revenue proxy, Capex surprise tracker | P0 |
| Stock Signal Module | 50/200 EMA system, RSI(14), volume confirmation, PEAD detection | P0 |
| Signal Aggregator | Composite scoring engine (-9 to +9) with action mapping | P0 |
| Backtesting Engine | vectorbt-based framework with transaction costs and benchmark comparison | P0 |
| Historical Dataset | 5+ years of clean, aligned daily data in Parquet format | P0 |
| Dashboard v0.2 | Signal scoring panel, individual stock technicals, backtest results viewer | P1 |
| Signal parameter documentation | Written rationale for every parameter choice | P1 |

### 1.4 Weekly Breakdown

| Week | Focus | Tasks | Output |
|------|-------|-------|--------|
| Week 5 | Sector Signals | SOX/SPX relative strength; TSMC revenue integration; sector momentum scoring | Sector signal module producing daily scores |
| Week 6 | Stock Signals | EMA system (50/200); RSI(14); volume breakout detection; PEAD framework | Per-stock signal scores for full universe |
| Week 7 | Aggregation + Backtest Engine | Composite scorer; vectorbt setup; first backtest runs; parameter sensitivity | Working backtest pipeline with benchmark comparison |
| Week 8 | Validation + Dashboard | Historical validation against known events; dashboard v0.2; documentation | Validated signal system, upgraded dashboard |

---

## 2. Prerequisites & Dependencies

### 2.1 New Python Dependencies

Add to `requirements.txt`:

```
# Backtesting (Month 2)
vectorbt>=0.26.0
ta>=0.11.0              # Technical analysis indicators
scipy>=1.12.0            # Statistical tests

# Data (Month 2)
duckdb>=0.10.0           # Parquet analytical queries
pyarrow>=15.0.0          # Parquet I/O
```

Install command:

```bash
pip install vectorbt ta scipy duckdb pyarrow
```

### 2.2 New Project Files

```
quantamental/
├── signals/
│   ├── macro.py             # (Month 1, unchanged)
│   ├── sector.py            # NEW: Sector timing signals
│   ├── stock.py             # NEW: Stock-level entry signals
│   └── aggregator.py        # NEW: Composite scoring engine
├── backtest/
│   ├── engine.py            # NEW: vectorbt wrapper
│   ├── strategies.py        # NEW: Strategy definitions
│   ├── analyzer.py          # NEW: Performance analytics
│   └── reports/             # NEW: Backtest result outputs
├── data/
│   └── parquet/
│       ├── daily_ohlcv.parquet    # NEW: Historical cold storage
│       ├── macro_history.parquet  # NEW: Macro indicator history
│       └── signals_history.parquet # NEW: Signal archive
└── scripts/
    ├── backfill.py          # UPDATED: Extended history load
    └── export_parquet.py    # NEW: QuestDB → Parquet export
```

---

## 3. Sector Timing Signal Design

The sector signal layer sits between macro (market-wide) and stock (individual security) signals. It answers the question: **is the AI Infra sector specifically outperforming or underperforming the broad market right now?**

### 3.1 Signal A: SOX/SPX Relative Strength

This is the primary sector momentum indicator. When semiconductor stocks outperform the S&P 500, it signals sustained demand for AI infrastructure.

| Parameter | Value |
|-----------|-------|
| Numerator | SOX (Philadelphia Semiconductor Index) or SMH ETF as proxy |
| Denominator | SPX (S&P 500 Index) or SPY ETF as proxy |
| Calculation | Ratio = SMH close / SPY close |
| Fast MA | 20-day EMA of ratio |
| Slow MA | 60-day EMA of ratio |
| Signal Logic | See scoring table below |

**Scoring Table:**

| Condition | Score | Interpretation |
|-----------|-------|----------------|
| 20 EMA > 60 EMA AND ratio at 20-day high | +2 | Strong sector momentum |
| 20 EMA > 60 EMA | +1 | Positive sector momentum |
| 20 EMA within 1% of 60 EMA | 0 | Neutral |
| 20 EMA < 60 EMA | -1 | Sector underperformance |
| 20 EMA < 60 EMA AND ratio at 20-day low | -2 | Strong sector weakness |

**Implementation pseudocode:**

```python
def calc_sox_spx_signal(smh_close: pd.Series, spy_close: pd.Series) -> int:
    ratio = smh_close / spy_close
    ema_20 = ratio.ewm(span=20).mean()
    ema_60 = ratio.ewm(span=60).mean()

    latest_ratio = ratio.iloc[-1]
    latest_20 = ema_20.iloc[-1]
    latest_60 = ema_60.iloc[-1]
    ratio_20d_high = ratio.iloc[-20:].max()
    ratio_20d_low = ratio.iloc[-20:].min()

    if latest_20 > latest_60:
        if latest_ratio >= ratio_20d_high * 0.99:
            return 2
        return 1
    elif abs(latest_20 - latest_60) / latest_60 < 0.01:
        return 0
    else:
        if latest_ratio <= ratio_20d_low * 1.01:
            return -2
        return -1
```

### 3.2 Signal B: TSMC Monthly Revenue Proxy

TSMC publishes monthly revenue data (typically by the 10th of each month). This is the single best real-time proxy for AI chip demand.

| Parameter | Value |
|-----------|-------|
| Data Source | TSMC IR website (manual entry or scraper) |
| Storage | SQLite table `tsmc_revenue` |
| Calculation | 3-month moving average of YoY growth rate |
| Update Frequency | Monthly (after TSMC reports) |

**Schema (SQLite):**

```sql
CREATE TABLE tsmc_revenue (
    month TEXT PRIMARY KEY,        -- '2026-01'
    revenue_twd_bn REAL,           -- Revenue in TWD billions
    yoy_growth REAL,               -- Year-over-year growth %
    ma3_yoy REAL,                  -- 3-month MA of YoY growth
    signal INT                     -- Derived signal score
);
```

**Scoring Table:**

| Condition | Score | Interpretation |
|-----------|-------|----------------|
| 3M MA YoY > 30% AND accelerating | +2 | Explosive demand growth |
| 3M MA YoY > 15% | +1 | Healthy demand |
| 3M MA YoY 0-15% | 0 | Moderate / normalizing |
| 3M MA YoY < 0% | -1 | Contraction |
| 3M MA YoY < 0% for 2+ consecutive months | -2 | Sustained downturn |

**Notes:**

- "Accelerating" means current month's 3M MA YoY > previous month's 3M MA YoY.
- Since this updates monthly, the signal persists between updates. Do not interpolate.
- If TSMC delays reporting, carry forward the previous month's signal.

### 3.3 Signal C: Hyperscaler Capex Surprise

This is an event-driven signal that activates during earnings season (approximately 4 times per year). It measures whether the largest AI infrastructure buyers are spending more or less than analysts expected.

| Parameter | Value |
|-----------|-------|
| Tracked Companies | META, MSFT, GOOGL, AMZN (the "Big 4" capex spenders) |
| Data Source | FMP API (earnings endpoint) or manual after earnings calls |
| Calculation | Average (Actual Capex - Consensus Capex) / Consensus Capex |
| Update Frequency | Quarterly (January, April, July, October) |

**Schema (SQLite):**

```sql
CREATE TABLE capex_surprise (
    quarter TEXT,                  -- '2026-Q1'
    company TEXT,                  -- 'META', 'MSFT', etc.
    actual_capex_bn REAL,
    consensus_capex_bn REAL,
    surprise_pct REAL,             -- (actual - consensus) / consensus
    PRIMARY KEY (quarter, company)
);
```

**Scoring Logic:**

```python
def calc_capex_surprise_signal(quarter: str) -> int:
    surprises = get_all_surprises(quarter)  # List of surprise_pct
    if len(surprises) < 2:
        return 0  # Insufficient data, neutral

    avg_surprise = sum(surprises) / len(surprises)

    if avg_surprise > 0.10:       # Average beat > 10%
        return 2
    elif avg_surprise > 0.0:      # Average beat > 0%
        return 1
    elif avg_surprise > -0.10:    # Average miss < 10%
        return -1
    else:                         # Average miss > 10%
        return -2
```

**Important:** This signal is "sticky" — the score persists for the full quarter until the next earnings cycle. Between earnings seasons, the last calculated score carries forward.

### 3.4 Signal D: AI API Pricing Trend (Qualitative → Quantitative)

This signal tracks the directional trend of inference pricing across major API providers. It is the most manual of all signals but captures a uniquely valuable demand/supply dynamic.

| Parameter | Value |
|-----------|-------|
| Tracked Providers | OpenAI, Anthropic, Google Vertex AI |
| Metric | Average $/million tokens across providers (input tokens, flagship model) |
| Storage | SQLite table `api_pricing` |
| Update Frequency | When any provider changes pricing (check weekly) |

**Schema (SQLite):**

```sql
CREATE TABLE api_pricing (
    date TEXT,
    provider TEXT,
    model TEXT,
    price_per_m_input REAL,        -- $/million input tokens
    price_per_m_output REAL,       -- $/million output tokens
    PRIMARY KEY (date, provider, model)
);
```

**Scoring Logic:**

| Condition | Score | Interpretation |
|-----------|-------|----------------|
| Prices rising or stable, no cuts in 3 months | +1 | Demand exceeds supply |
| Prices dropping < 30% per quarter | 0 | Normal efficiency gains |
| Prices dropping > 30% per quarter | -1 | Potential demand/margin concern |
| Prices dropping > 50% per quarter | -2 | Major efficiency shock (DeepSeek scenario) |

**Note:** A price drop of > 50% in a single quarter almost certainly coincides with a major architecture breakthrough. This signal should trigger immediate review of the upstream compute thesis.

### 3.5 Sector Signal Composite

The sector layer produces a composite score from -8 to +8 by summing all four signals. However, for the Month 2 implementation, signals B, C, and D will likely have limited history, so the composite initially relies heavily on Signal A (SOX/SPX). This is acceptable — the system is designed to incorporate more signals as data accumulates.

```python
def sector_composite() -> int:
    a = sox_spx_signal()          # -2 to +2
    b = tsmc_revenue_signal()     # -2 to +2
    c = capex_surprise_signal()   # -2 to +2
    d = api_pricing_signal()      # -2 to +2
    return a + b + c + d          # -8 to +8
```

---

## 4. Stock-Level Signal Design

The stock signal layer evaluates individual securities within the universe. It answers: **is this specific stock at a favorable entry point right now, or should I wait?**

### 4.1 Signal 1: Dual EMA System (50/200 Day)

This is the core trend-following indicator for medium-term investing.

| Parameter | Value |
|-----------|-------|
| Fast EMA | 50-day Exponential Moving Average |
| Slow EMA | 200-day Exponential Moving Average |
| Data Source | daily_ohlcv (QuestDB), close price |

**Scoring Table:**

| Condition | Score | Name |
|-----------|-------|------|
| Price > 50 EMA > 200 EMA | +2 | Strong uptrend |
| Price > 200 EMA, but < 50 EMA | +1 | Pullback in uptrend (potential entry) |
| Price oscillating around both EMAs | 0 | Trendless / consolidation |
| Price < 200 EMA, but > 50 EMA | -1 | Recovery attempt (watch) |
| Price < 50 EMA < 200 EMA | -2 | Strong downtrend |

**Golden Cross / Death Cross events:**

- Golden Cross (50 EMA crosses above 200 EMA): generates a one-time event flag `GOLDEN_CROSS` stored in a separate events table. This is a candidate for accelerated entry.
- Death Cross (50 EMA crosses below 200 EMA): generates `DEATH_CROSS` event flag. This is a thesis review trigger — not an automatic sell, but requires immediate reassessment.

**Implementation:**

```python
def calc_ema_signal(close: pd.Series) -> dict:
    ema_50 = close.ewm(span=50).mean()
    ema_200 = close.ewm(span=200).mean()

    latest_price = close.iloc[-1]
    latest_50 = ema_50.iloc[-1]
    latest_200 = ema_200.iloc[-1]

    # Trend score
    if latest_price > latest_50 > latest_200:
        score = 2
    elif latest_price > latest_200 >= latest_50:
        score = 1
    elif latest_price < latest_50 < latest_200:
        score = -2
    elif latest_price < latest_200 <= latest_50:
        score = -1
    else:
        score = 0

    # Cross detection
    prev_50 = ema_50.iloc[-2]
    prev_200 = ema_200.iloc[-2]
    event = None
    if prev_50 <= prev_200 and latest_50 > latest_200:
        event = "GOLDEN_CROSS"
    elif prev_50 >= prev_200 and latest_50 < latest_200:
        event = "DEATH_CROSS"

    return {"score": score, "event": event, "ema_50": latest_50, "ema_200": latest_200}
```

### 4.2 Signal 2: RSI(14) — Relative Strength Index

RSI measures the speed and magnitude of recent price changes to detect overbought or oversold conditions.

| Parameter | Value |
|-----------|-------|
| Period | 14 trading days |
| Library | `ta.momentum.RSIIndicator` from `ta` package |

**Scoring Table:**

| RSI Range | Score | Interpretation | Action Implication |
|-----------|-------|----------------|-------------------|
| < 25 | +2 | Deeply oversold | Strong entry signal (if thesis intact) |
| 25 - 35 | +1 | Oversold | Potential entry zone |
| 35 - 65 | 0 | Neutral | No signal from RSI |
| 65 - 75 | -1 | Overbought | Caution, do not initiate new positions |
| > 75 | -2 | Deeply overbought | Do not buy; consider trimming if overweight |

**Important nuance for trending stocks:** In a strong uptrend (EMA score = +2), RSI can stay above 60 for extended periods. An RSI of 65-75 in this context is not necessarily bearish — it reflects momentum. The signal aggregator accounts for this by applying a "trend adjustment" (see Section 5).

### 4.3 Signal 3: Volume Confirmation

Volume validates whether a price move is genuine or likely to reverse. A breakout on high volume is more reliable than one on low volume.

| Parameter | Value |
|-----------|-------|
| Average Volume | 20-day simple moving average of daily volume |
| Threshold | 1.5x average volume = "high volume" |

**Scoring Logic:**

```python
def calc_volume_signal(close: pd.Series, volume: pd.Series) -> int:
    avg_vol_20 = volume.rolling(20).mean()
    latest_vol = volume.iloc[-1]
    daily_return = close.pct_change().iloc[-1]

    vol_ratio = latest_vol / avg_vol_20.iloc[-1]

    if vol_ratio > 1.5 and daily_return > 0.02:
        return 1    # Bullish breakout confirmed by volume
    elif vol_ratio > 1.5 and daily_return < -0.02:
        return -1   # Bearish breakdown confirmed by volume
    else:
        return 0    # Volume not informative today
```

**Note:** This signal is inherently noisy on a daily basis. It is most useful as a confirmation layer — when other signals suggest a trade, volume confirmation increases conviction. A volume score of 0 does not argue against a trade.

### 4.4 Signal 4: Post-Earnings Announcement Drift (PEAD)

PEAD is one of the most well-documented anomalies in finance (Bernard & Thomas 1989). Stocks that beat earnings expectations tend to continue drifting upward for 3-6 weeks after the announcement, and vice versa.

| Parameter | Value |
|-----------|-------|
| Data Source | FMP earnings endpoint or manual tracking |
| Detection Window | 1-3 trading days after earnings release |
| Drift Window | 3-6 weeks post-earnings |
| Threshold | EPS surprise > 5% = "beat"; < -5% = "miss" |

**Schema (SQLite):**

```sql
CREATE TABLE earnings_events (
    symbol TEXT,
    report_date TEXT,
    fiscal_quarter TEXT,
    actual_eps REAL,
    consensus_eps REAL,
    surprise_pct REAL,
    revenue_surprise_pct REAL,
    guidance TEXT,                  -- 'RAISED', 'MAINTAINED', 'LOWERED', 'NONE'
    pead_signal INT,               -- Derived signal score
    signal_expiry TEXT,            -- Date when PEAD signal expires (6 weeks out)
    PRIMARY KEY (symbol, report_date)
);
```

**Scoring Logic:**

| Condition | Score | Duration |
|-----------|-------|----------|
| EPS beat > 10% AND guidance raised | +2 | 6 weeks |
| EPS beat > 5% | +1 | 4 weeks |
| EPS within ±5% | 0 | N/A |
| EPS miss > 5% | -1 | 4 weeks |
| EPS miss > 10% AND guidance lowered | -2 | 6 weeks |

**Decay rule:** PEAD signals decay linearly over their duration. A +2 signal that lasts 6 weeks becomes +1 after 3 weeks and 0 after 6 weeks. This prevents stale earnings data from driving decisions indefinitely.

```python
def calc_pead_signal(symbol: str, today: date) -> int:
    event = get_latest_earnings(symbol)
    if event is None:
        return 0

    days_since = (today - event.report_date).days
    if days_since > event.duration_days:
        return 0  # Signal expired

    decay_factor = 1.0 - (days_since / event.duration_days)
    raw_score = event.pead_signal
    return round(raw_score * decay_factor)
```

### 4.5 Stock Signal Composite

Each stock receives a composite score from -7 to +7:

```python
def stock_composite(symbol: str) -> int:
    ema = calc_ema_signal(symbol)["score"]      # -2 to +2
    rsi = calc_rsi_signal(symbol)               # -2 to +2
    vol = calc_volume_signal(symbol)             # -1 to +1
    pead = calc_pead_signal(symbol, today)       # -2 to +2
    return ema + rsi + vol + pead               # -7 to +7
```

---

## 5. Signal Aggregation Engine

### 5.1 Architecture

The aggregator combines all three signal layers into a single decision framework. It produces a composite score and maps it to a concrete action.

```
┌─────────────────────────────────────────┐
│         Signal Aggregation Engine        │
│                                          │
│  Macro Regime Score     (-8 to +8)  ×1.0 │
│  Sector Timing Score    (-8 to +8)  ×0.8 │
│  Stock-Level Score      (-7 to +7)  ×0.6 │
│                                          │
│  Weighted Composite = Σ(score × weight)  │
│  Normalized to -9 to +9 range            │
│                                          │
│  Output: Score + Regime + Action         │
└─────────────────────────────────────────┘
```

### 5.2 Weighting Rationale

| Layer | Raw Range | Weight | Weighted Range | Why This Weight |
|-------|-----------|--------|----------------|-----------------|
| Macro | -8 to +8 | 1.0 | -8.0 to +8.0 | Market regime is the dominant return driver; fighting the macro trend is the #1 cause of losses |
| Sector | -8 to +8 | 0.8 | -6.4 to +6.4 | Sector momentum has strong predictive power (3-12 months) but is subordinate to macro |
| Stock | -7 to +7 | 0.6 | -4.2 to +4.2 | Individual stock signals are noisiest; useful for timing within an established trend, not for overriding macro/sector |

**Maximum weighted range:** -18.6 to +18.6, normalized to -9 to +9.

### 5.3 Normalization

```python
def normalize_composite(macro: int, sector: int, stock: int) -> float:
    weighted = macro * 1.0 + sector * 0.8 + stock * 0.6
    max_possible = 8 * 1.0 + 8 * 0.8 + 7 * 0.6  # = 18.6
    normalized = (weighted / max_possible) * 9
    return round(max(-9, min(9, normalized)), 1)
```

### 5.4 Trend Adjustment for RSI

As noted in Section 4.2, RSI in a strong uptrend can appear overbought without being truly bearish. The aggregator applies a correction:

```python
def adjusted_rsi_score(rsi_score: int, ema_score: int) -> int:
    # In strong uptrend, soften overbought RSI penalty
    if ema_score == 2 and rsi_score == -1:
        return 0   # Overbought in strong uptrend = neutral, not bearish
    # In strong downtrend, soften oversold RSI bonus
    if ema_score == -2 and rsi_score == 1:
        return 0   # Oversold in strong downtrend = neutral, not bullish (catching knives)
    return rsi_score
```

### 5.5 Action Mapping

| Normalized Score | Regime | Portfolio Action |
|------------------|--------|------------------|
| +7 to +9 | STRONG_BUY | Accelerate batch entries; deploy reserve cash |
| +4 to +6 | BUY | Proceed with scheduled entries; no hesitation |
| +1 to +3 | MILD_BUY | Maintain positions; selective new entries only |
| -1 to +0 | NEUTRAL | Hold; no new entries |
| -4 to -2 | MILD_SELL | Pause entries; tighten stops by 5% |
| -7 to -5 | SELL | Reduce exposure 25-50% |
| -9 to -8 | STRONG_SELL | Emergency de-risk; reduce to minimum; activate all hedges |

### 5.6 Signal Conflict Resolution

When signals conflict across layers, the following hierarchy applies:

1. **Macro override:** If macro regime is RISK_OFF (score < -4), no new long positions regardless of sector/stock signals. The market tide is the strongest force.

2. **Sector confirmation required for new entries:** Do not open new positions if sector score is negative, even if individual stock looks attractive. An attractive stock in a weak sector is a value trap candidate.

3. **Stock signal is for timing only:** Stock signals determine when to enter an already-approved name, not whether to enter it. The "what to buy" comes from fundamental analysis; the "when to buy" comes from the stock signal.

---

## 6. Backtesting Engine Design

### 6.1 Technology Choice: vectorbt

vectorbt is chosen over alternatives for the following reasons:

| Criterion | vectorbt | backtrader | Custom pandas |
|-----------|----------|------------|---------------|
| Speed | Very fast (vectorized numpy) | Slow (event-driven loop) | Medium |
| Parameter sweep | Built-in, parallelized | Manual loops | Manual loops |
| Statistics | Comprehensive built-in | Basic | Manual |
| Learning curve | Medium | High | Low |
| Flexibility | High for systematic strategies | High for complex logic | Maximum |
| Visualization | Built-in plotly charts | matplotlib | Manual |

### 6.2 Engine Architecture

```
backtest/
├── engine.py          # Core backtesting wrapper
├── strategies.py      # Strategy signal generation functions
├── analyzer.py        # Performance metrics & reporting
└── reports/           # Output directory for results
```

**engine.py core interface:**

```python
class BacktestEngine:
    def __init__(self, data: pd.DataFrame, initial_cash: float = 100_000):
        """
        Args:
            data: DataFrame with DatetimeIndex, columns = tickers,
                  values = adjusted close prices
            initial_cash: Starting portfolio value
        """
        self.data = data
        self.initial_cash = initial_cash

    def run_signal_backtest(
        self,
        entries: pd.DataFrame,    # Boolean DataFrame: True = enter
        exits: pd.DataFrame,      # Boolean DataFrame: True = exit
        fees: float = 0.001,      # 10bps round-trip
        slippage: float = 0.001,  # 10bps slippage estimate
        freq: str = "d",
    ) -> "BacktestResult":
        """Run backtest using vectorbt Portfolio.from_signals"""
        ...

    def run_parameter_sweep(
        self,
        param_grid: dict,         # e.g., {"ema_fast": [20,50], "ema_slow": [100,200]}
        strategy_fn: callable,     # Function that takes params → (entries, exits)
        metric: str = "sharpe",   # Optimization target
    ) -> pd.DataFrame:
        """Sweep parameter space, return ranked results"""
        ...
```

### 6.3 Transaction Cost Model

Realistic backtesting requires accounting for costs that erode returns:

| Cost Component | Assumption | Rationale |
|----------------|------------|-----------|
| Commission | $0 | Most US brokers are zero-commission |
| Spread / Slippage | 0.05% per trade | Large-cap liquid stocks, market orders |
| Market impact | 0.05% per trade | Conservative for position sizes < $100K |
| Total round-trip cost | 0.20% | Entry + exit combined |

These costs are applied as `fees=0.001` in vectorbt (0.1% per side, 0.2% round-trip).

### 6.4 Benchmark Comparison

Every backtest must be compared against relevant benchmarks:

| Benchmark | Ticker | Purpose |
|-----------|--------|---------|
| S&P 500 | SPY | Broad market comparison |
| Nasdaq 100 | QQQ | Tech-heavy comparison |
| Semiconductors | SMH | Direct sector comparison |
| Buy-and-hold universe | Equal-weight all holdings | Strategy vs passive exposure |

### 6.5 Required Metrics

Every backtest report must include:

| Metric | Formula / Method | Minimum Threshold |
|--------|-----------------|-------------------|
| Total Return | (Final - Initial) / Initial | > benchmark |
| Annualized Return | (1 + total)^(252/days) - 1 | > benchmark |
| Sharpe Ratio | (Rp - Rf) / σp | > 1.0 |
| Sortino Ratio | (Rp - Rf) / σ_downside | > 1.5 |
| Max Drawdown | Max peak-to-trough decline | < 20% |
| Calmar Ratio | Annual return / Max drawdown | > 1.0 |
| Win Rate | Profitable trades / Total trades | > 50% |
| Profit Factor | Gross profit / Gross loss | > 1.5 |
| Average Trade Duration | Mean holding period in days | 20-90 days |
| Number of Trades | Total round-trip trades | Enough for statistical significance (>30) |

### 6.6 Anti-Overfitting Protocol

Backtesting is dangerous because it's easy to find strategies that work on historical data but fail live. The following protocol is mandatory:

1. **Train/Test Split:** Use data from 2020-2024 for development ("in-sample") and 2025-2026 for validation ("out-of-sample"). Never optimize on the out-of-sample period.

2. **Walk-Forward Analysis:** Divide the in-sample period into rolling 12-month windows. Train on 12 months, test on the next 3 months, slide forward. A strategy must be profitable in at least 60% of walk-forward windows.

3. **Parameter Stability:** If the optimal EMA fast period is 50, the strategy should also work reasonably with 40 and 60. If performance collapses with small parameter changes, the strategy is overfit.

4. **Transaction Count Filter:** Any strategy with fewer than 30 trades in-sample is statistically unreliable. Do not trust its metrics.

5. **Multiple Testing Correction:** If you test 100 parameter combinations, expect 5 to appear significant at p < 0.05 by pure chance. Use Bonferroni or Holm correction, or apply the Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014).

6. **Paper Trade First:** After in-sample + out-of-sample validation, paper trade for at least 2-4 weeks before committing real capital to a new signal.

---

## 7. Historical Data Management

### 7.1 Backfill Requirements

| Dataset | History Needed | Source | Storage |
|---------|---------------|--------|---------|
| Daily OHLCV (universe) | 2019-01-01 to present | Polygon.io | QuestDB (recent 6 months) + Parquet (full history) |
| SMH / SPY daily | 2019-01-01 to present | Polygon.io | Same as above |
| VIX daily | 2019-01-01 to present | FRED (VIXCLS) | Same as above |
| 10Y yield | 2019-01-01 to present | FRED (DGS10) | Same as above |
| Fed balance sheet | 2019-01-01 to present | FRED (WALCL) | Same as above |
| Credit spread | 2019-01-01 to present | FRED (BAMLC0A0CM) | Same as above |

**Why 2019?** This gives you 7+ years of data covering multiple regimes: pre-COVID bull market, COVID crash and recovery, 2022 rate hiking cycle, 2023-2024 AI bull run, DeepSeek shock (Jan 2025), and current period.

### 7.2 Backfill Script Design

```python
# scripts/backfill.py

def backfill_polygon(tickers: list, start: str, end: str):
    """
    Fetch historical daily data from Polygon.io for all tickers.

    Rate limit handling:
    - Free tier: 5 requests/minute
    - Insert 12-second delay between requests
    - Total time for 27 tickers × 7 years ≈ 6 minutes

    Steps:
    1. For each ticker, call Polygon aggs endpoint
    2. Transform to standard OHLCV schema
    3. Write to QuestDB via ILP (fast batch insert)
    4. Export to Parquet for cold storage
    """
    ...

def backfill_fred(series_ids: list, start: str, end: str):
    """
    Fetch historical macro data from FRED.

    No significant rate limits on FRED API.
    """
    ...
```

### 7.3 Parquet Export Strategy

```python
# scripts/export_parquet.py

def export_to_parquet():
    """
    Export QuestDB tables to Parquet files for:
    1. Cold storage backup
    2. DuckDB analytical queries
    3. Notebook research use

    Schedule: Weekly (Sunday night)
    """
    query = "SELECT * FROM daily_ohlcv ORDER BY ts"
    df = pd.read_sql(query, questdb_connection)
    df.to_parquet("data/parquet/daily_ohlcv.parquet", index=False)
```

---

## 8. QuestDB Schema Extensions

The following new tables are added to QuestDB for Month 2:

### 8.1 Sector Signals Table

```sql
CREATE TABLE IF NOT EXISTS sector_signals (
    ts TIMESTAMP,
    sox_spx_ratio DOUBLE,
    sox_spx_ema20 DOUBLE,
    sox_spx_ema60 DOUBLE,
    sox_spx_signal INT,
    tsmc_signal INT,
    capex_signal INT,
    api_pricing_signal INT,
    sector_composite INT
) TIMESTAMP(ts) PARTITION BY MONTH;
```

### 8.2 Stock Signals Table

```sql
CREATE TABLE IF NOT EXISTS stock_signals (
    symbol SYMBOL CAPACITY 64 INDEX,
    ts TIMESTAMP,
    close DOUBLE,
    ema_50 DOUBLE,
    ema_200 DOUBLE,
    ema_signal INT,
    rsi_14 DOUBLE,
    rsi_signal INT,
    volume_ratio DOUBLE,
    volume_signal INT,
    pead_signal INT,
    stock_composite INT
) TIMESTAMP(ts) PARTITION BY MONTH;
```

### 8.3 Composite Signals Table

```sql
CREATE TABLE IF NOT EXISTS composite_signals (
    ts TIMESTAMP,
    macro_score INT,
    sector_score INT,
    -- Per-stock scores stored in stock_signals table
    -- This table stores portfolio-level aggregation
    avg_stock_score DOUBLE,
    weighted_composite DOUBLE,
    normalized_score DOUBLE,
    regime STRING,
    action STRING
) TIMESTAMP(ts) PARTITION BY MONTH;
```

### 8.4 Events Table

```sql
CREATE TABLE IF NOT EXISTS signal_events (
    symbol SYMBOL CAPACITY 64 INDEX,
    ts TIMESTAMP,
    event_type STRING,             -- GOLDEN_CROSS, DEATH_CROSS, EARNINGS_BEAT, etc.
    details STRING,
    signal_impact INT              -- -2 to +2
) TIMESTAMP(ts) PARTITION BY MONTH;
```

---

## 9. Dashboard Upgrade (v0.2)

### 9.1 New Panels

The dashboard expands from 4 panels (Month 1) to 7 panels:

| Panel | Content | New in v0.2 |
|-------|---------|-------------|
| A: Macro Regime | 4 macro indicators + regime badge | Unchanged |
| B: Portfolio Overview | Position table with P&L | Unchanged |
| C: Stop-Loss Monitor | Distance to stop per position | Unchanged |
| D: Signal History | Composite score time series (60 days) | Unchanged |
| **E: Sector Signals** | SOX/SPX ratio chart + sector composite score | **New** |
| **F: Stock Technicals** | Per-stock chart with EMA overlay, RSI subplot, volume bars | **New** |
| **G: Backtest Results** | Latest backtest summary: returns, Sharpe, drawdown, equity curve | **New** |

### 9.2 Stock Technical Chart Specification (Panel F)

This is the most complex new panel. It should render for a user-selected ticker from a dropdown.

**Chart layout (3 subplots, vertically stacked):**

| Subplot | Content | Height Ratio |
|---------|---------|-------------|
| Price chart | Candlestick or line + 50 EMA (blue) + 200 EMA (red) + entry/exit markers | 60% |
| RSI | RSI(14) line + 30/70 horizontal reference lines + overbought/oversold shading | 20% |
| Volume | Bar chart, colored green (up days) / red (down days), with 20-day MA line | 20% |

**Implementation:** Use `plotly` with `make_subplots(rows=3, shared_xaxes=True)` for synchronized zooming and panning.

### 9.3 Backtest Results Panel (Panel G)

| Element | Content |
|---------|---------|
| Equity curve | Line chart comparing strategy vs SPY vs SMH |
| Key metrics table | Return, Sharpe, Sortino, Max Drawdown, Win Rate, Profit Factor |
| Parameter summary | Which signal parameters produced these results |
| Last updated | Timestamp of most recent backtest run |

---

## 10. Automation Updates

### 10.1 Updated Pipeline Schedule

The daily pipeline from Month 1 is extended with new steps:

| Time (ET) | Step | Module | New in Month 2 |
|-----------|------|--------|----------------|
| 4:30 PM | Fetch market data | polygon_client.py | |
| 4:35 PM | Fetch macro data | fred_client.py | |
| 4:40 PM | Calculate macro signals | macro.py | |
| **4:42 PM** | **Calculate sector signals** | **sector.py** | **New** |
| **4:44 PM** | **Calculate stock signals** | **stock.py** | **New** |
| **4:46 PM** | **Run signal aggregation** | **aggregator.py** | **New** |
| 4:50 PM | Update portfolio state | tracker.py | |
| 4:55 PM | Check stop-losses + send alerts | stoploss.py | |
| 5:00 PM | Refresh dashboard | app.py | |

Total pipeline duration: approximately 30-45 seconds.

### 10.2 Weekly Backtest Job

A weekly backtest run is added to validate that signals continue to perform on recent data:

```
# Run backtest every Sunday at 10:00 AM
0 10 * * 0 cd /path/to/quantamental && python backtest/engine.py --mode weekly >> logs/backtest.log 2>&1
```

The weekly backtest:
1. Pulls latest 6 months of data from QuestDB
2. Runs the current signal configuration against this window
3. Compares performance vs SPY/SMH benchmark
4. Logs results to `backtest/reports/weekly_YYYYMMDD.json`
5. If Sharpe ratio drops below 0.5, sends a Telegram alert

---

## 11. Testing & Validation

### 11.1 Signal Unit Tests

Each signal module must have unit tests covering:

| Test Category | What It Verifies |
|---------------|-----------------|
| Edge cases | Signal at exact threshold values (RSI = 25, 35, 65, 75) |
| Direction | Bullish input produces positive score, bearish produces negative |
| Range | Output is within expected bounds (-2 to +2 for individual signals) |
| Monotonicity | More bullish input never produces a lower score than less bullish input |
| Null handling | Missing data returns 0 (neutral), not an error |
| Stale data | Old PEAD signals decay correctly over time |

**Example test:**

```python
def test_rsi_scoring_boundaries():
    assert calc_rsi_signal_from_value(24.9) == 2
    assert calc_rsi_signal_from_value(25.0) == 1
    assert calc_rsi_signal_from_value(35.0) == 0
    assert calc_rsi_signal_from_value(65.0) == 0
    assert calc_rsi_signal_from_value(65.1) == -1
    assert calc_rsi_signal_from_value(75.0) == -2
```

### 11.2 Historical Regime Validation

Run the complete signal system against historical data and verify it correctly identifies known events:

| Date Range | Known Event | Expected Regime |
|------------|-------------|-----------------|
| Feb 2020 | Pre-COVID rally | RISK_ON (macro + sector both positive) |
| Mar 2020 | COVID crash | RISK_OFF (VIX > 80, credit spreads blow out) |
| Apr-Nov 2020 | Recovery rally | Transition to RISK_ON |
| Jan-Oct 2022 | Rate hiking cycle | RISK_OFF (10Y yield surging) |
| Nov 2022 - Jul 2023 | AI bull market begins | RISK_ON (SOX outperforming) |
| Jan 2025 | DeepSeek shock | Brief RISK_OFF spike, then recovery |
| Current | Evaluate real-time | Should match your qualitative judgment |

If the signal system fails to identify more than one of these events correctly, the parameters need adjustment before trusting it for live decisions.

### 11.3 Backtest Validation Checklist

| Check | Purpose | Pass Condition |
|-------|---------|---------------|
| In-sample Sharpe > 1.0 | Strategy has edge | Must pass |
| Out-of-sample Sharpe > 0.5 | Edge persists | Must pass |
| Walk-forward hit rate > 60% | Robustness | Must pass |
| Max drawdown < 25% | Risk constraint | Must pass |
| Parameter sensitivity | No overfitting | Score stable ±20% across nearby params |
| Number of trades > 30 | Statistical significance | Must pass |
| Transaction costs included | Realistic returns | Fees = 0.001 per side |

---

## 12. Risks & Contingency

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Polygon free tier insufficient for backfill | Medium | Delays backfill | Upgrade to $29/mo; or use yfinance for historical-only backfill then switch to Polygon for live |
| vectorbt installation issues | Low | Delays backtest setup | Fall back to manual pandas-based backtesting for Week 7; migrate later |
| Signals appear profitable in-sample but fail out-of-sample | High | Wasted effort on overfit strategy | Strict anti-overfitting protocol (Section 6.6); accept that most strategies will fail |
| TSMC revenue data hard to automate | Medium | Signal B degrades to manual | Accept manual monthly update; do not over-engineer a scraper for one data point |
| Feature creep — trying to add ML, options, etc. | High | Month 2 not delivered | Ruthlessly defer to Month 3+; document ideas but do not implement |
| Imposter syndrome — signals "don't feel right" | Medium | Paralysis | Trust the backtest, not your feelings; paper trade first if needed |

---

## 13. Success Criteria

Month 2 is considered successful if all of the following are true by Day 60:

1. **Sector signal module** produces a daily sector composite score for at least 20 consecutive trading days without errors.

2. **Stock signal module** produces per-stock signals (EMA, RSI, volume, PEAD) for the full universe daily.

3. **Signal aggregator** produces a normalized composite score (-9 to +9) and maps it to a regime and action.

4. **Historical backfill** contains 5+ years of daily OHLCV data for all universe tickers, stored in both QuestDB and Parquet.

5. **Backtesting engine** can run a complete EMA crossover strategy backtest on the full universe within 60 seconds, producing all required metrics.

6. **At least one backtest** passes the anti-overfitting protocol (Section 6.6) on both in-sample and out-of-sample data.

7. **Dashboard v0.2** renders all 7 panels correctly, including the stock technical chart with EMA overlay and RSI.

8. **Parameter documentation** exists for every signal threshold, explaining why that value was chosen (even if the reason is "industry convention" or "initial guess, will refine").

**Items explicitly deferred to Month 3:** VaR/CVaR risk engine, Kelly position sizing automation, correlation matrix monitoring, portfolio optimization (PyPortfolioOpt / Black-Litterman), advanced hedging strategies.

---

*End of Month 2 Design Specification*

*This document is confidential and for personal use only. Not investment advice.*
