# Wheel 操盘逻辑与过程（供检验）

**生成时间：** 2026-04-17
**当前版本：** v3（回测验证）
**工作目录：** `C:\Users\belle\OneDrive\Desktop\AI Trading\.claude\worktrees\competent-jones`

---

## 1. 触发节奏

| 时机 | 触发 | 动作 |
|------|------|------|
| 开盘 9:31 ET (21:31 SGT) | GitHub Actions cron | 发邮件 + 跑 `wheel_once.py` |
| 开盘后每 5 分钟 | GitHub Actions | 跑 `wheel_once.py`（决策 + 可能下单） |
| 收盘 4:05 ET (04:05 SGT 次日) | GitHub Actions | 发邮件 + 记录 snapshot |
| 每天 8:24 SGT | scheduled-tasks | 健康检查 + shadow rotation |

**实盘决策入口：** `wheel_once.py` → `WheelStrategy.run_cycle()`

---

## 2. 状态机（4 个阶段）

```
          ┌─────────┐
          │  IDLE   │ ◄──── 起点（无持仓，无订单）
          └────┬────┘
               │ 卖 Cash-Secured Put
               ▼
          ┌──────────┐
          │SHORT_PUT │ ◄──── 等待 Put 到期
          └────┬─────┘
               │
     ┌─────────┴─────────┐
     │                   │
  OTM 到期             被行权 (S<Strike)
     │                   │
     ▼                   ▼
 回 IDLE            ┌──────────┐
                    │LONG_STOCK│ ◄──── 持有 100 股
                    └────┬─────┘
                         │ 卖 Covered Call
                         ▼
                    ┌──────────┐
                    │SHORT_CALL│ ◄──── 等待 Call 到期
                    └────┬─────┘
                         │
                ┌────────┴────────┐
                │                 │
             OTM 到期         被 Call 走 (S>Strike)
                │                 │
                ▼                 ▼
         回 LONG_STOCK         回 IDLE
         (继续卖 Call)      (股票被收走)
```

**阶段判定代码：** `strategy/wheel_strategy.py:get_phase()`
**逻辑：** 查 Alpaca 实盘持仓 + 未成交订单，自动推断当前阶段。

---

## 3. 当前配置参数（config/settings.py）

| 参数 | 值 | 含义 |
|------|----|----|
| `WHEEL_SYMBOL` | `TSLA`（从 `wheel_symbol.json` 动态读） | 当前活跃标的 |
| `WHEEL_TARGET_DELTA` | `0.25` | 目标 Delta（Put 为 -0.25）|
| `WHEEL_MIN_DTE` | `1` | 最短到期天数 |
| `WHEEL_MAX_DTE` | `5` | 最长到期天数 |
| `WHEEL_CONTRACTS` | `2` | 默认合约数（Kelly 会动态调整）|
| `WHEEL_CHECK_INTERVAL` | `300`s | 监控间隔 |

---

## 4. 每次 run_cycle() 完整流程

### Step 1：判断当前阶段
`get_phase()` 查 Alpaca 实盘 → 返回 IDLE / SHORT_PUT / LONG_STOCK / SHORT_CALL

### Step 2：跑 evaluator 评估是否换标的
**文件：** `strategy/wheel_evaluator.py:evaluate_and_maybe_plan()`

判定规则（**按顺序**）：

#### 2a. 健康检查当前标的
调用 `wheel_health_check.run_health_check(symbol)` 返回 `GO / CAUTION / STOP`

6 维检查：
1. 10 日实际波动率
2. 最大单日跌幅
3. 隐含波动率 IV
4. 权利金年化收益率
5. 购买力充足性
6. 期权流动性

#### 2b. STOP → 必须切换
- 调用 `_find_best_alternative()` 在全 32 只池子里扫描
- 优先：评分最高 + GO 健康的标的
- Fallback：QQQ / MSFT / AAPL / GOOGL / COST / UNH / SPY
- 登记切换计划（IDLE 时立即生效，非 IDLE 等周五到期）

#### 2c. CAUTION → 10% 阈值即换
- 调用 `_find_better_alternative()` 找 Top1
- Top1 评分 ≥ baseline × 1.10 → 登记切换

#### 2d. GO + IDLE → 主动优化（15% 阈值）
- **前提：上一周期必须赚钱**（`_last_cycle_was_profitable()`）
  - 证据 1：`metrics/trade_journal.csv` 最近一次 exit 是 `expired_otm` 或 pnl > 0
  - 证据 2：Alpaca 近 14 天已平仓订单 STO − BTC > 0
  - 证据 3：当前权益 ≥ 上日
- 如上一周期亏损 → **不换**（先在原标的恢复）
- 如赚钱 + Top1 评分 ≥ baseline × 1.15 + Top1 健康 GO → 切换

#### 2e. GO + 持仓中 → 不动
等本周期到期再评估。

### Step 3：执行切换（如有计划）
`maybe_switch()` 检查 3 个条件同时满足：
1. 切换计划存在
2. 今天 ≥ 触发日期
3. 当前阶段 = IDLE

满足则更新 `wheel_symbol.json`，返回 `(old, new)`，**跳过本轮**，等下一 cron 用新标的。

### Step 4：根据阶段执行交易

#### 4a. 如果 IDLE → 卖 Put

**3 个过滤器**（全部通过才卖）：
```
pre_open_put_checks(symbol):
  ├─ check_earnings()     — 5 天内有财报 → 跳过
  ├─ check_ma_trend()     — 股价 < MA50 → 跳过
  └─ check_realized_vol() — 年化 RV > 90% → 跳过
```

**选期权合约：**
```
select_put(DTE 1-5 天):
  ├─ 扫描所有 Put 合约
  ├─ 过滤无 Greeks / 无报价的
  └─ 选 Delta 最接近 -0.25 的合约
```

**计算合约数（Kelly 公式）：**
```
kelly_contracts(cash, strike, premium):
  p = 0.85 (胜率估计)
  q = 0.15
  b = premium / strike  (收益风险比)
  Kelly = max(p - q/b, 0) × 0.25  (分数 Kelly)
  允许现金 = cash × Kelly
  合约数 = int(允许现金 / (strike × 100))
  范围：[1, WHEEL_CONTRACTS × 2] = [1, 4]
```

**下单：** `sell_to_open(contract, qty, mid_price)` — Alpaca Paper API

**记录日志：** `trade_journal.log_entry()` 写入 24 字段 CSV

#### 4b. 如果 LONG_STOCK → 卖 Covered Call

**1 个过滤器**（仅财报）：
```
pre_open_call_checks(symbol):
  └─ check_earnings() — 5 天内有财报 → 跳过
```
（不过滤 MA Trend，因为已经持股必须卖 Call 回收权利金）

**选期权：**
```
select_call(cost_basis):
  ├─ 扫描所有 Call 合约
  ├─ 过滤 strike < cost_basis（不锁定亏损）
  └─ 选 Delta 最接近 +0.25 的合约
```

**合约数 = 持股数 / 100**（最多 `WHEEL_CONTRACTS × 2`）

#### 4c. 如果 SHORT_PUT / SHORT_CALL → 等待到期

不做任何操作，只记录日志。

---

## 5. 风控层（独立于策略）

**文件：** `strategy/risk_manager.py`

| 规则 | 阈值 | 动作 |
|------|------|------|
| 日内熔断 | 当日亏损 ≥ 5% | 停止所有新开仓 |
| 单笔止损 | 单笔亏损 ≥ 2% | 触发止损 |
| ATR 止损 | 入场价 ± ATR×2 | 自动挂止损单 |
| 持仓上限 | 5 个并行持仓 | 拒绝新开仓 |
| 单标的上限 | 占权益 10% | 限制仓位 |

**注意：** Wheel 策略的仓位限制由 Kelly 和现金充足性主导，风控层是兜底。

---

## 6. 候选标的池

**Wheel 池（32 只，用于 CSP 扫描）：**
```
Mag 7：        AAPL, MSFT, GOOGL, META, AMZN, NVDA, TSLA
科技龙头：     AMD, AVGO, TSM, NFLX, ORCL, CRM, ADBE
半导体：       MU, QCOM, INTC, AMAT, ARM
高活跃：       PLTR, HOOD, COIN, MSTR
防御：         COST, UNH
光子/CXL：     MRVL, LITE, COHR, IPGP
服务器：       SMCI
水冷：         XYL
内存 ETF：     DRAM
```

**实际评分扫描：** `wheel_scanner.scan_candidates()`
- 按综合评分排序
- 评分维度：年化权利金 + IV 甜蜜区 + MA50 + 单日最大跌幅

---

## 7. 关键决策阈值总表

| 决策 | 阈值 | 触发效果 |
|------|------|---------|
| **卖 Put 过滤** | | |
| 财报窗口 | 5 天内 | 跳过 |
| MA50 趋势 | 股价 < MA50 | 跳过 |
| 极端波动 | 年化 RV > 90% | 跳过 |
| **合约数（Kelly）** | | |
| 胜率估计 p | 0.85 | 参与计算 |
| 分数 Kelly | 0.25 (×¼) | 保守化 |
| 合约范围 | [1, 4] | 限制 |
| **轮换决策** | | |
| STOP 健康 | 必换 | 找最高分 GO 标的 |
| CAUTION + Top1 改善 | ≥ 10% | 切换 |
| GO + IDLE + 赚钱 + Top1 改善 | ≥ 15% | 切换 |
| GO + 持仓中 | 不动 | 等到期 |
| **赚钱周期判定** | | |
| trade_journal 最近 exit pnl | > 0 | 视为赚钱 |
| 近 14 天 STO−BTC | > 0 | 视为赚钱 |

---

## 8. 最近一次实际决策（现在）

**TSLA 当前状态：**
- 阶段: `SHORT_PUT`
- 持仓: TSLA260417P00352500 qty=-2
- 到期: **2026-04-17 明天**
- 权利金已收: $476（入场 $2.38）
- 当前价: $0.06（97.5% 已赚）

**Evaluator 决策：**
```
✅ TSLA GO+profit(近 14 天 2 笔净盈利 +$746) -
    Top1 AVGO 仅 +4% (< 15% 阈值)，保持
```

**明天到期后的预期流程：**
1. TSLA 当前 $388.90 > strike $352.50 → Put OTM 作废
2. Phase 变回 `IDLE`，赚 $12 剩余时间价值
3. 触发 evaluator，用修复后的逻辑比较 TSLA vs Top1
4. 差距 < 15% → 继续卖 TSLA 下一张 Put
5. 差距 ≥ 15% → 切换标的

---

## 9. 所有关键文件

| 文件 | 职责 |
|------|------|
| `wheel_once.py` | GitHub Actions 触发的单次执行入口 |
| `strategy/wheel_strategy.py` | 主策略，状态机 + run_cycle |
| `strategy/wheel_evaluator.py` | 每轮评估是否换标的 |
| `strategy/wheel_filters.py` | 过滤器（earnings / MA / vol）+ Kelly |
| `strategy/wheel_switch.py` | 切换计划的登记/触发 |
| `wheel_scanner.py` | 候选扫描 + 评分 |
| `wheel_health_check.py` | 6 维健康检查（GO/CAUTION/STOP）|
| `config/settings.py` | 全局参数 |
| `wheel_symbol.json` | 当前活跃标的（动态更新）|
| `metrics/trade_journal.py` | 交易日志（影响"赚钱判定"）|

---

## 10. 需要你检验的核心假设

请检查以下假设是否符合你的预期：

- [ ] **目标 Delta 0.25** 是否合适？（越大越激进、权利金越高）
- [ ] **DTE 1-5 天** 是否合适？（短 = 高 theta 衰减但容易被行权）
- [ ] **Kelly 胜率估计 85%** 是否合理？（真实历史胜率约 75-83%）
- [ ] **卖 Put 必须 MA50 上方** 是否太保守？（错过反弹机会）
- [ ] **GO+IDLE 15% 阈值** 是否合适？（越低越频繁换 → 交易成本升）
- [ ] **CAUTION 10% 阈值** 是否合适？
- [ ] **财报前 5 天完全不开仓** 是否太保守？
- [ ] **极端波动 RV > 90% 才停** 是否太宽？
- [ ] **"上轮亏损锁死"逻辑** 是否会卡在坏标的上？
- [ ] **默认 2 张合约** 是否适合 $100k 账户？

---

告诉我哪些假设要改，或者有没有看出逻辑漏洞。
