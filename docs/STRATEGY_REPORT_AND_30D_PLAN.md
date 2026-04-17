# AI Trading 完整策略报告 & 30 天进化计划

**版本：** v1.0
**编制日期：** 2026-04-16
**作者：** AI Trading Bot
**周期：** 2026-04-17 ～ 2026-05-16

---

## 目录

1. [系统现状总览](#1-系统现状总览)
2. [策略详细说明](#2-策略详细说明)
3. [当前基线指标](#3-当前基线指标)
4. [已知弱点诊断](#4-已知弱点诊断)
5. [30 天进化计划](#5-30-天进化计划)
6. [衡量与回滚机制](#6-衡量与回滚机制)
7. [长期愿景](#7-长期愿景-90-天展望)

---

## 1. 系统现状总览

### 1.1 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                     trading.py (统一入口)                    │
│  17 个交互式选项：账户/实盘/回测/工具/通知                   │
└───────────────┬─────────────────────────────────────────────┘
                │
    ┌───────────┼────────────┬──────────────┐
    ↓           ↓            ↓              ↓
┌─────────┐ ┌────────┐ ┌──────────┐ ┌────────────┐
│ EMA+RSI │ │ Wheel  │ │ Account  │ │   Email    │
│   +ML   │ │ Option │ │Dashboard │ │ Automation │
│  策略   │ │  策略  │ │          │ │            │
└─────────┘ └────────┘ └──────────┘ └────────────┘
     ↓           ↓
┌─────────────────────┐    ┌─────────────────────┐
│  Risk Manager       │    │  Wheel Evaluator    │
│  (5% 日内熔断)      │    │  (健康检查+切换)    │
└─────────────────────┘    └─────────────────────┘
     ↓                            ↓
┌─────────────────────────────────────────────┐
│         Alpaca Paper Trading API            │
│       (账户/订单/期权链/历史数据)           │
└─────────────────────────────────────────────┘
```

### 1.2 核心模块

| 模块 | 文件 | 职责 |
|------|------|------|
| 入口 | `trading.py` | 统一交互式菜单 |
| 实盘 EMA+RSI | `main.py` | 长时间运行的股票交易机器人 |
| Wheel 单次 | `wheel_once.py` | GitHub Actions 触发的单次执行 |
| Wheel 监控 | `run_wheel_csco.py` | 持续循环监控 |
| 邮件 | `email_summary.py` + `utils/emailer.py` | 开盘/收盘自动邮件 |
| 标的池 | `wheel_scanner.py` | 32 只科技股 + 防御股 |
| 健康评估 | `wheel_health_check.py` | 6 维评分 |
| 自动切换 | `strategy/wheel_evaluator.py` | STOP→必须切换 |
| 胜率追踪 | `metrics/baseline_tracker.py` | 每日快照 + 周报 |
| 验证工具 | `scripts/validate_universe.py` | 流动性数据验证 |

### 1.3 已完成的工程改进

| 完成日期 | 改进 | 影响 |
|---------|------|------|
| 2026-04-16 | 创建 `trading.py` 统一入口（17 选项） | 用户体验 ↑ |
| 2026-04-16 | 邮件通知系统（Gmail SMTP + Markdown→HTML） | 自动化 ↑ |
| 2026-04-16 | 定时任务（开盘 21:31 SGT、收盘 04:05 SGT） | 自动化 ↑ |
| 2026-04-16 | 标的池扩展（5 只 → 32 只科技股） | 多样性 ↑ |
| 2026-04-16 | 标的验证脚本（流动性 + 期权双检） | 数据驱动 ↑ |
| 2026-04-16 | 胜率追踪模块 + 周报集成 | 可观测性 ↑ |

---

## 2. 策略详细说明

### 2.1 策略 A：EMA+RSI+ML 股票交易

**适用场景：** 日内/短线股票交易（1Min K线）

#### 买入条件（必须全部满足）

| # | 条件 | 阈值 |
|---|------|------|
| 1 | EMA 金叉 | EMA(9) 上穿 EMA(21) |
| 2 | 趋势确认 | 价格 > SMA(50) |
| 3 | RSI 区间 | 40 ≤ RSI ≤ 65 |
| 4 | MACD 动能 | MACD 柱状图 > 0 |
| 5 | ML 过滤 | RF 模型方向一致 + 置信度 ≥ 60% |

#### 卖出条件（必须全部满足）

| # | 条件 | 阈值 |
|---|------|------|
| 1 | EMA 死叉 | EMA(9) 下穿 EMA(21) |
| 2 | 趋势确认 | 价格 < SMA(50) |
| 3 | RSI 区间 | 35 ≤ RSI ≤ 60 |
| 4 | MACD 动能 | MACD 柱状图 < 0 |

#### 强制平仓
RSI > 70 或 RSI < 30 → 立即平仓（避免追高/抄底）

---

### 2.2 策略 B：Wheel 期权策略（核心策略）

**状态机：**

```
   ┌──────────┐
   │   IDLE   │ ← ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐
   └────┬─────┘                              │
        │ 卖 Cash-Secured Put                │
        ↓                                    │
   ┌──────────┐                              │
   │SHORT_PUT │ ← 等待到期/被行权            │
   └────┬─────┘                              │
        │ 被行权 → 接到 100 股                 │
        ↓                                    │
   ┌──────────┐                              │
   │LONG_STOCK│                              │
   └────┬─────┘                              │
        │ 卖 Covered Call                    │
        ↓                                    │
   ┌──────────┐                              │
   │SHORT_CALL│ → 被行权 → 股票被收走 ──────┘
   └──────────┘
```

#### 期权选择标准

| 项目 | 卖 Put | 卖 Call |
|------|-------|---------|
| 目标 Delta | -0.25 | +0.25 |
| DTE 范围 | 1-5 天 | 1-5 天 |
| 最低 Strike | 无限制 | ≥ 持仓成本 |

#### 入场过滤器（卖 Put 前）
1. **财报过滤** — 5 天内财报 → 跳过
2. **MA 趋势** — 价格 < MA50 → 跳过
3. **波动率上限** — 年化 RV > 90% → 跳过

#### 仓位计算（Kelly 公式）
```
b = 权利金 / Strike
Kelly = max(p − q/b, 0) × 0.25  (p=0.85, q=0.15)
最大合约数 = min(int(Kelly × 现金 / Strike / 100), 4)
```

---

### 2.3 风控规则

| 规则 | 阈值 | 触发动作 |
|------|------|---------|
| 日内熔断 | 当日亏损 ≥ 5% | 停止所有新开仓 |
| 单笔止损 | 单笔亏损 ≥ 2% | 触发止损 |
| ATR 止损 | 入场价 ± ATR×2 | 自动挂止损单 |
| 持仓上限 | 5 个并行 | 拒绝新开仓 |
| 单标的上限 | 占权益 10% | 限制仓位 |

---

### 2.4 标的池

#### Wheel 池（32 只）

```
Mag 7：       AAPL, MSFT, GOOGL, META, AMZN, NVDA, TSLA  (7)
科技龙头：    AMD, AVGO, TSM, NFLX, ORCL, CRM, ADBE       (7)
半导体：      MU, QCOM, INTC, AMAT, ARM                    (5)
高活跃：      PLTR, HOOD, COIN, MSTR                       (4)
防御股：      COST, UNH                                    (2)
ETF：         DRAM                                          (1)
光子/CXL：    MRVL, LITE, COHR, IPGP                       (4)
服务器：      SMCI                                         (1)
水冷：        XYL                                          (1)
```

#### 自动切换优先级
```
MSFT → AAPL → GOOGL → QQQ → META → AMZN → NVDA → AMD
→ AVGO → TSM → NFLX → ORCL → CRM
→ MRVL → LITE → COHR → IPGP
→ SMCI → XYL → DRAM
→ COST → UNH → PLTR → TSLA
```

---

## 3. 当前基线指标

### 3.1 TSLA Wheel 回测（2023-2025，3 年 751 天）

| 变体 | 年化 | 胜率 | 最大回撤 | Sharpe |
|------|------|------|---------|--------|
| 01 基线 | +4.4% | 75% | 28.2% | 0.30 |
| 02 财报过滤 | +8.0% | 83% | 28.6% | 0.47 |
| **10 组合最优** | **+10.7%** | **79%** | 26.5% | **0.62** |
| 12 生产配置 | +4.4% | 80% | **8.3%** | 0.54 |

### 3.2 实盘账户（2026-04-16）

| 指标 | 值 |
|------|---|
| 当前权益 | $100,725.28 |
| 现金 | $100,749.28 |
| 购买力 | $30,249.28 |
| 当日 P&L | +$348.00 (+0.35%) |
| 当前 Wheel 标的 | TSLA |
| 持仓 | Short Put: TSLA260417P00352500 (qty=-2) |

---

## 4. 已知弱点诊断

### 4.1 严重问题（影响胜率 > 5%）

| # | 弱点 | 影响 | 优先级 |
|---|------|------|--------|
| 1 | **过滤器静态** — IV/MA/Vol 阈值固定，不适配市场状态 | -3~5% 胜率 | 🔴 高 |
| 2 | **缺乏 Roll 机制** — Put 进 ITM 直接接货 | -5~8% 胜率 | 🔴 高 |
| 3 | **ML 模型不更新** — 训练后从不重训 | 时间衰减 | 🟡 中 |
| 4 | **固定 Delta 0.25** — 不分高/低 IV 环境 | -2~4% 胜率 | 🟡 中 |
| 5 | **无市场情绪输入** — 只看技术面 | 提前预警缺失 | 🟢 低 |

### 4.2 工程问题

| # | 问题 | 影响 | 优先级 |
|---|------|------|--------|
| 1 | API key 易过期且无监控 | 静默失败 | 🔴 高 |
| 2 | 邮件失败无重试机制 | 通知漏失 | 🟡 中 |
| 3 | worktree 与主项目 .env 不同步 | 配置漂移 | 🟢 低 |

---

## 5. 30 天进化计划

### 5.1 总体路线

```
Week 1 (4/17-4/23)：基础建设  — 让数据流转起来
Week 2 (4/24-4/30)：信号增强  — 提升决策质量
Week 3 (5/1-5/7)：风控优化   — 降低回撤
Week 4 (5/8-5/14)：智能化    — 自动学习/适配
Day 29-30 (5/15-5/16)：评估与调优
```

**核心目标：**
- 月度胜率：80% (基线) → 85% (目标)
- 月度年化：10.7% → 15%+
- 最大回撤：< 15%
- Sharpe：0.62 → 1.0+

---

### 5.2 Week 1 — 基础建设（4/17 ～ 4/23）

**主题：让数据流转起来，建立可观测性**

| 日 | 日期 | 任务 | 产出 | 验证 |
|----|------|------|------|------|
| Mon | 4/17 | 修复 worktree/.env 同步问题，建立部署脚本 | `scripts/sync_env.sh` | 一键同步 |
| Tue | 4/18 | 增强 `baseline_tracker` 记录每笔交易细节 | `metrics/trade_journal.py` | 自动记录入场/出场/原因 |
| Wed | 4/19 | 邮件失败重试 + Telegram 备份通知 | 修改 `utils/emailer.py` | 失败 3 次告警 |
| Thu | 4/20 | API key 健康监控，Daily heartbeat | `scripts/health_check.py` | 每日 ping |
| Fri | 4/21 | 收盘报告新增"本周复盘"模块 | 修改 `wheel_summary.py` | 周五邮件含复盘 |
| Sat | 4/22 | 跑完整 5 标的回测 (TSLA/NVDA/MSFT/AAPL/SPY) | `reports/baseline_5sym.md` | 基线对比表 |
| Sun | 4/23 | Week 1 复盘 + Week 2 准备 | 写 `docs/week1_review.md` | 自评 |

**Week 1 验收标准：**
- ✅ 每笔交易自动记录到 `metrics/data/trade_journal.csv`
- ✅ 邮件失败有备份通知
- ✅ Daily 健康检查工作
- ✅ 基线数据库建立

---

### 5.3 Week 2 — 信号增强（4/24 ～ 4/30）

**主题：让策略更聪明**

| 日 | 日期 | 任务 | 预期胜率 ↑ |
|----|------|------|-----------|
| Mon | 4/24 | **改进 1：IV Rank 过滤** — IV 百分位 > 30% 才卖 | +3% |
| Tue | 4/25 | 回测验证 IV Rank 改进，A/B 比较 | — |
| Wed | 4/26 | **改进 2：动态 Delta** — VIX 高低自适应 | +2% |
| Thu | 4/27 | 回测验证动态 Delta，单独测 + 组合测 | — |
| Fri | 4/28 | **改进 3：Roll 机制** — Put 进 ITM 5% 时 roll | +5% |
| Sat | 4/29 | Roll 机制完整回测（关键改进！） | — |
| Sun | 4/30 | Week 2 复盘 + 决定哪些改进进入生产 | — |

**实现细节：**

#### IV Rank 过滤（`strategy/iv_rank_filter.py`）
```python
def iv_rank(symbol: str, lookback_days: int = 252) -> float:
    """计算 IV 百分位 (0-100)"""
    iv_history = fetch_iv_history(symbol, lookback_days)
    current_iv = iv_history[-1]
    return (sorted(iv_history).index(current_iv) / len(iv_history)) * 100
```
**阈值：** IV Rank < 30 → 跳过（权利金不够）

#### 动态 Delta（修改 `wheel_strategy.py`）
```python
vix = get_vix()
if vix < 15:    target_delta = 0.30   # 低波动期，靠近一些
elif vix > 25:  target_delta = 0.15   # 高波动期，远离一些
else:           target_delta = 0.25   # 默认
```

#### Roll 机制（新建 `strategy/option_roll.py`）
触发条件：
- Put 进 ITM 5% 以上
- 剩余 DTE > 2 天
- 滚动到下周 + Strike 下移 10%

**Week 2 验收标准：**
- ✅ 每个改进单独有 A/B 回测数据
- ✅ 至少 1 个改进通过验证（年化 +2% 以上）
- ✅ Roll 机制可工作（即使先用 paper 测试）

---

### 5.4 Week 3 — 风控优化（5/1 ～ 5/7）

**主题：让回撤更可控**

| 日 | 日期 | 任务 | 影响 |
|----|------|------|------|
| Mon | 5/1 | **改进 4：动态仓位** — 基于近 30 天回撤调整 Kelly fraction | -3% MaxDD |
| Tue | 5/2 | 回测改进 4 | — |
| Wed | 5/3 | **改进 5：SPY 200MA 过滤** — 大盘破位时全停 | -5% MaxDD |
| Thu | 5/4 | 回测改进 5 | — |
| Fri | 5/5 | **改进 6：Profit Taking** — 50% 利润 → 提前 BTC | +1% 胜率 |
| Sat | 5/6 | 综合改进回测，最终确定生产参数 | — |
| Sun | 5/7 | Week 3 复盘 + 生产部署清单 | — |

#### 动态 Kelly 实现
```python
def adaptive_kelly_fraction(recent_drawdown: float) -> float:
    """近期回撤越大，仓位越保守"""
    if recent_drawdown > 0.15:  return 0.10  # 减半
    if recent_drawdown > 0.10:  return 0.15
    if recent_drawdown > 0.05:  return 0.20
    return 0.25  # 默认
```

#### SPY 200MA 过滤
```python
def market_ok_to_sell_put() -> bool:
    spy = get_bars("SPY", days=210)
    sma200 = spy["close"].rolling(200).mean().iloc[-1]
    return spy["close"].iloc[-1] >= sma200
```

**Week 3 验收标准：**
- ✅ 综合最佳配置下 MaxDD < 15%
- ✅ Sharpe ≥ 1.0
- ✅ 年化 ≥ 12%

---

### 5.5 Week 4 — 智能化升级（5/8 ～ 5/14）

**主题：让系统能自我学习**

| 日 | 日期 | 任务 | 类型 |
|----|------|------|------|
| Mon | 5/8 | **Walk-Forward 重训框架** — 每周自动重训 ML | 学习闭环 |
| Tue | 5/9 | 回测验证 walk-forward vs 一次性训练 | — |
| Wed | 5/10 | **策略效果追踪** — 识别哪些过滤器在赚钱/亏钱 | 自评估 |
| Thu | 5/11 | **新闻情绪集成** — Alpaca News API → 单股情绪分 | 新维度 |
| Fri | 5/12 | 情绪分作为额外过滤器 | — |
| Sat | 5/13 | 全系统集成测试 + Paper 实盘对比 | — |
| Sun | 5/14 | Week 4 复盘 | — |

#### 自动重训框架（`learning/weekly_retrain.py`）
```python
def weekly_retrain():
    """每周日 23:00 自动重训"""
    end = date.today()
    start = end - timedelta(days=730)  # 用最近 2 年数据
    df = fetch_bars(SYMBOLS, start, end)
    new_model = RFClassifier()
    new_model.train(df)

    # A/B 比较：新旧模型在最近 30 天
    if new_model.score > old_model.score * 1.05:
        new_model.save()
        send_email("ML 模型已升级", report)
    else:
        send_email("ML 重训未通过验证", report)
```

#### 情绪集成（`strategy/sentiment_filter.py`）
```python
def sentiment_score(symbol: str, lookback_hours: int = 24) -> float:
    """聚合 24 小时新闻情绪 (-1 到 +1)"""
    news = fetch_news(symbol, hours=lookback_hours)
    scores = [analyze_headline(n.headline) for n in news]
    return mean(scores) if scores else 0
```

**过滤逻辑：** 卖 Put 前如果情绪 < -0.5（强负面） → 跳过

**Week 4 验收标准：**
- ✅ ML 模型可自动重训
- ✅ 周报新增"过滤器贡献分析"
- ✅ 至少 1 周完整运行无人工干预

---

### 5.6 最后两天（5/15 ～ 5/16）：评估 & 调优

| 日 | 任务 |
|----|------|
| Thu 5/15 | 跑 30 天总结回测，对比 4/16 基线 |
| Fri 5/16 | 写最终报告 `docs/30day_results.md`，决定下个 30 天计划 |

---

## 6. 衡量与回滚机制

### 6.1 每周强制度量

每周日生成自动报告 → 通过邮件发送：

```markdown
## Week N 进度报告
- 本周完成任务: X/7
- 实盘胜率: XX% (vs 上周 XX%)
- 实盘年化: XX% (vs 基线 4.4%)
- 最大回撤: XX%
- 新增改进通过验证: [列表]
- 下周计划: [列表]
```

### 6.2 红线指标（触发回滚）

| 指标 | 红线 | 行动 |
|------|------|------|
| 月度胜率 | < 70% | 回退到上一稳定版本 |
| 单周回撤 | > 10% | 暂停所有新策略，复盘 |
| 连续亏损 | 5 单 | 停止 24 小时，人工审核 |
| 系统错误 | > 10 次/天 | 邮件告警 + 暂停 |

### 6.3 版本控制

每个改进单独 commit + tag：
```
v1.0-baseline           ← 4/16 基准
v1.1-iv-rank-filter
v1.2-dynamic-delta
v1.3-option-roll
v1.4-adaptive-kelly
v1.5-spy-filter
v1.6-profit-taking
v1.7-walk-forward
v1.8-sentiment
v2.0-30day-final        ← 5/16 总成果
```

回滚命令：`git checkout v1.X-name`

---

## 7. 长期愿景（90 天展望）

### Day 31-60：横向扩展
- 多账户并行（不同策略 A/B）
- 加入期货 ES/NQ Wheel 测试
- 引入第二个 ML 模型（XGBoost）做集成

### Day 61-90：智能化深化
- 接入实时社交情绪（Reddit r/wallstreetbets API）
- 强化学习实验（Q-Learning 选择最优 Delta）
- 自动论文复现（每月跟一个顶会期权策略论文）

### 终极目标（Year 1）
- 月度胜率稳定 ≥ 85%
- 年化 ≥ 25%
- 最大回撤 ≤ 10%
- 全自动运行（人工干预 < 1 次/月）

---

## 附录 A：关键文件位置

| 用途 | 路径 |
|------|------|
| 主入口 | `trading.py` |
| 策略报告（本文） | `docs/STRATEGY_REPORT_AND_30D_PLAN.md` |
| 周复盘 | `docs/week*_review.md` |
| 回测报告 | `reports/wheel_backtest_*.md` |
| 标的验证 | `reports/universe_validation.json` |
| 每日快照 | `metrics/data/daily_snapshot.csv` |
| 交易日志 | `metrics/data/closed_trades.csv` |
| 标的池配置 | `wheel_scanner.py` (WHEEL_UNIVERSE) |
| 优先级配置 | `strategy/wheel_evaluator.py` |

## 附录 B：每日工作清单（你的责任）

每日（自动）：
- [ ] 收盘后查看邮件摘要
- [ ] 检查是否有红线告警

每周（5 分钟）：
- [ ] 周日查看 Week N 进度报告
- [ ] 批准/拒绝待执行的改进
- [ ] 确认下周计划

每月（30 分钟）：
- [ ] 与基线对比，评估总进度
- [ ] 决定是否调整目标

---

**END OF REPORT**

_此文档为活文档，每周日由系统自动更新进度。_
