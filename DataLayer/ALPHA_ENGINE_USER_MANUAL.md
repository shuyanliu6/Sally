# Quantamental Alpha Engine 用户手册

> 这是本项目的中文操作手册，面向日常使用者。它覆盖环境准备、数据更新、Alpha 排名、回测、Dashboard 查看和常见问题。
>
> 当前系统是**投资决策辅助工具**，不是自动交易系统。所有排名、权重和信号都需要结合你的研究判断使用。

---

## 1. 系统能做什么

这个项目现在包含四个核心层：

1. **数据层**
   - 拉取股票 OHLCV 数据。
   - 拉取 FRED 宏观数据。
   - 使用 QuestDB 保存行情和信号。
   - 使用 SQLite 保存组合、交易日志和手动录入数据。

2. **信号层**
   - Macro：10Y 美债、VIX、Fed balance sheet、credit spread。
   - Sector：SMH/SPY 相对强弱、TSMC 月收入、CapEx surprise、AI API pricing。
   - Stock：EMA、RSI、Volume、PEAD。

3. **Alpha Engine**
   - 对 AI-infra candidate universe 进行横截面排名。
   - 输出 `alpha_score`、`rank`、`bucket`、建议目标权重。
   - 默认是 long-only、2-8 周持有周期、透明加权评分，不使用黑箱 ML。

4. **Dashboard**
   - 查看宏观 regime。
   - 查看 sector signals。
   - 查看单股技术信号。
   - 查看组合和止损。
   - 查看最新 Alpha 排名。

---

## 2. 第一次使用前准备

进入项目目录：

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer
```

创建并激活 Python 虚拟环境：

```bash
python -m venv .venv
source .venv/bin/activate
```

安装依赖：

```bash
pip install -e ".[dev]"
```

复制环境变量模板：

```bash
cp quantamental/config/.env.example quantamental/config/.env
```

编辑 `quantamental/config/.env`，至少填入：

```bash
POLYGON_API_KEY=你的_polygon_key
FRED_API_KEY=你的_fred_key
```

启动 QuestDB：

```bash
docker compose up -d
```

QuestDB 控制台地址：

```text
http://localhost:9000
```

---

## 3. 日常操作流程

### 3.1 每日更新数据和信号

进入 `quantamental` 目录：

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer/quantamental
```

运行完整 pipeline：

```bash
python scripts/daily_pipeline.py --step all
```

这个命令会依次运行：

- 拉取市场数据。
- 拉取宏观数据。
- 计算 macro signals。
- 计算 sector signals。
- 计算 stock signals。
- 更新 fundamentals。
- 更新 portfolio P&L。
- 检查 stop-loss alerts。

如果中途失败，可以恢复：

```bash
python scripts/daily_pipeline.py --resume
```

如果你想强制重跑当天所有步骤：

```bash
python scripts/daily_pipeline.py --step all --force
```

---

## 4. 运行 Alpha Engine

Alpha Engine 的目标是回答：

> 当前 AI-infra 股票池里，哪些股票最值得优先关注？

运行最新 Alpha 排名：

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer/quantamental
python scripts/run_alpha.py --asof 2026-04-29
```

如果不传 `--asof`，默认使用今天：

```bash
python scripts/run_alpha.py
```

默认行为：

- 读取 QuestDB 里的行情、macro signals、sector signals、stock signals。
- 生成每只候选股的 feature row。
- 计算 `alpha_score`。
- 生成排名和 bucket。
- 生成建议目标权重。
- 保存结果到：

```text
data/parquet/alpha/
```

默认不会写入 QuestDB。

如果你明确想把 Alpha 排名写入 QuestDB：

```bash
python scripts/run_alpha.py --asof 2026-04-29 --persist-db
```

---

## 5. Alpha 输出怎么看

Alpha 排名主要字段：

| 字段 | 含义 |
|---|---|
| `symbol` | 股票代码 |
| `rank` | 排名，1 是最高 |
| `alpha_score` | 0-100 分的 Alpha 分数 |
| `bucket` | 分组：`TOP_BUY`、`BUY`、`HOLD`、`AVOID` |
| `target_weight` | 建议目标权重 |
| `target_cash` | 建议现金比例 |
| `deployment_cap` | 当前环境允许的最大部署比例 |
| `new_buys_allowed` | 当前是否允许新增买入 |
| `score_components` | 分数组成，方便解释 |

Bucket 含义：

| Bucket | 含义 |
|---|---|
| `TOP_BUY` | 当前最强候选，优先研究 |
| `BUY` | 可以考虑进入或加仓 |
| `HOLD` | 继续观察或持有 |
| `AVOID` | 暂不优先考虑 |

注意：`TOP_BUY` 不等于必须买入。它的意思是“系统认为值得你优先研究”。

---

## 6. Portfolio Construction 规则

当前 V1 组合规则：

- Long-only。
- 每周再平衡，而不是每天交易。
- 默认持有 top 8-12 只股票。
- 单只股票最大目标权重：15%。
- 单只股票最小目标权重：5%。
- 如果 macro regime 是 `RISK_OFF`：
  - 阻止新增买入。
  - 目标现金比例至少 50%。
- 如果 sector composite 为负：
  - 总仓位上限降低到约 50-70%。

这些规则的目的不是最大化仓位，而是避免在坏环境里盲目追高。

---

## 7. 回测 Alpha Engine

回测命令：

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer/quantamental
python scripts/backtest_alpha.py --start 2025-01-01 --end 2026-04-01
```

设置交易成本和滑点：

```bash
python scripts/backtest_alpha.py --start 2025-01-01 --end 2026-04-01 --cost-bps 15
```

默认比较对象：

- Alpha strategy。
- Equal-weight candidates。
- `SPY`。
- `QQQ`。
- `SMH`。

回测输出指标：

| 指标 | 含义 |
|---|---|
| `cagr` | 年化收益率 |
| `sharpe` | 夏普比率 |
| `sortino` | Sortino ratio |
| `max_drawdown` | 最大回撤 |
| `calmar` | CAGR / 最大回撤 |
| `avg_turnover` | 平均换手 |
| `hit_rate` | 日收益为正比例 |
| `information_coefficient` | 排名和未来收益的相关性 |
| `avg_holding_period_days` | 平均持有周期估算 |

回测报告会保存到：

```text
data/parquet/alpha/backtests/
```

---

## 7.1 Alpha Performance Report

从 fund manager 角度，最重要的问题是：

> `TOP_BUY` / `BUY` bucket 在未来 20/40 个交易日，是否真的跑赢 `SMH` 和等权候选池？

生成 performance report：

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer/quantamental
python scripts/alpha_performance.py --start 2025-01-01 --end 2026-04-01
```

默认是 weekly evaluation，也就是每周重建一次排名，然后观察后续表现。

输出内容：

| 字段 | 含义 |
|---|---|
| `top_buy_buy_avg_excess` | `TOP_BUY` + `BUY` 相对 `SMH` 的平均超额收益 |
| `avoid_avg_excess` | `AVOID` 相对 `SMH` 的平均超额收益 |
| `top_minus_avoid` | 好 bucket 和差 bucket 的表现差 |
| `mean_rank_ic` | Alpha 分数和未来超额收益的秩相关 |
| `win_rate_vs_SMH` | 跑赢 `SMH` 的比例 |
| `win_rate_vs_equal_weight` | 跑赢等权候选池的比例 |

报告保存位置：

```text
data/parquet/alpha/performance/
```

如果 `top_minus_avoid` 长期为正，说明 ranking 有一定有效性。  
如果 `mean_rank_ic` 接近 0 或长期为负，说明当前 alpha score 的排序能力不足。

---

## 8. 打开 Dashboard

进入 `quantamental` 目录：

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer/quantamental
```

启动 Dashboard：

```bash
streamlit run dashboard/app.py
```

浏览器会打开：

```text
http://localhost:8501
```

Dashboard 面板：

| Panel | 内容 |
|---|---|
| A | Macro Regime |
| F | Sector Signals |
| G | Stock Technicals |
| H | Alpha Ranker |
| B | Portfolio Overview |
| C | Stop-Loss Monitor |
| D | Signal History |
| E | Candidate List Editor |

Panel H 会读取最新保存的 Alpha 排名。如果没有看到内容，先运行：

```bash
python scripts/run_alpha.py
```

---

## 9. 管理 Candidate List

查看当前候选池：

```bash
python scripts/manage_candidates.py --show
```

你也可以在 Dashboard 的 Panel E 里编辑 candidate list。

Alpha Engine V1 默认只对 candidate list 里的 AI-infra 股票排名，而不是全市场选股。

---

## 10. 数据健康检查

检查数据是否健康：

```bash
python scripts/check_data.py
```

检查更长窗口：

```bash
python scripts/check_data.py --days 60
```

如果 QuestDB 没启动，相关检查会失败。先运行：

```bash
docker compose up -d
```

---

## 11. 推荐日常节奏

每天收盘后：

```bash
python scripts/daily_pipeline.py --step all
python scripts/run_alpha.py
```

然后打开 Dashboard：

```bash
streamlit run dashboard/app.py
```

每周一次：

```bash
python scripts/backtest_alpha.py --start 2025-01-01 --end 2026-04-01
```

每月一次：

- 检查 candidate list。
- 更新 TSMC revenue。
- 更新 hyperscaler CapEx。
- 更新 AI API pricing。
- 复盘 Alpha 排名和实际表现。

---

## 12. 常见问题

### Q1：运行 Alpha Engine 没有输出？

先确认你已经跑过：

```bash
python scripts/daily_pipeline.py --step all
```

然后确认 QuestDB 里有：

- `daily_ohlcv`
- `stock_signals`
- `regime_signals`
- `sector_signals`

### Q2：Dashboard Panel H 没有数据？

先生成 Alpha 排名：

```bash
python scripts/run_alpha.py
```

Panel H 默认读取：

```text
data/parquet/alpha/alpha_ranks_latest.parquet
```

### Q3：Alpha 排名可以直接买入吗？

不建议。Alpha 排名是研究优先级，不是交易指令。

推荐流程：

1. 看 `TOP_BUY` 和 `BUY`。
2. 检查基本面和事件风险。
3. 检查当前 macro regime。
4. 检查 sector composite。
5. 再决定是否进入、加仓或等待。

### Q4：回测结果很好，是否说明系统有效？

不一定。你需要继续检查：

- 是否有 survivorship bias。
- 是否有 look-ahead bias。
- 是否交易成本设置太低。
- 是否只在某一段行情有效。
- 是否相对 `SMH`、`QQQ`、equal-weight candidates 真的有改善。

### Q5：什么时候可以信任 Alpha Engine？

至少满足：

- 多个时间窗口回测有效。
- Walk-forward 测试有效。
- 相比 `SMH` 和 equal-weight candidates 有稳定改善。
- 最大回撤可以接受。
- 换手率和交易成本现实。

---

## 13. 测试

运行完整测试：

```bash
cd /Users/shuyan/Desktop/nothing/Sally/DataLayer
python -m pytest
```

只测试 Alpha Engine：

```bash
python -m pytest quantamental/tests/test_alpha_engine.py
```

当前期望状态：

```text
232 passed, 8 skipped
```

其中 skipped 通常是因为 QuestDB 没有运行。

---

## 14. 重要提醒

- 这是研究和决策辅助系统，不是自动交易系统。
- Alpha 分数不是确定性预测。
- 回测通过之前，不要把它当成真实 alpha。
- 即使回测通过，也要控制仓位和回撤。
- 保留交易日志，持续复盘系统是否真的改善决策。
