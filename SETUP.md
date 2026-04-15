# Wheel 策略 — 配置 & 运行指南

一共 **3 种跑法**，按 "从看样子 → 本地真数据 → 生产" 递进。

---

## 🚀 路径 1：离线 Demo（10 秒，不用 key）

**目的**：看一眼 wheel_summary 输出长什么样。全是合成数据，不连 Alpaca。

```bash
pip install alpaca-py pandas numpy loguru python-dotenv pydantic pytz requests
python wheel_demo.py open
```

**输出**：一份完整的 markdown 摘要（账户、期权筛选、候选股扫描、LEAPS、健康检查），标的叫 `DEMO`，末尾会标 `[DEMO MODE]` 提示。

**可以吗**：跑不出 "真实状态"，但能验证所有代码路径、样式、逻辑分支。

---

## 🔑 路径 2：本地真数据（5 分钟，需要 Alpaca Paper key）

### Step 1 — 拿 Alpaca Paper key

1. 浏览器打开 **`https://app.alpaca.markets/paper/dashboard/overview`**（注意 `app`，不是 `docs`）
2. 登陆（没账号就 Sign up，免费、不用入金）
3. 确认左上角是 **Paper** 模式（不要选 Live）
4. 右侧找 **"Your API Keys"** 板块 → 点 **Generate New Key** 按钮
5. 弹窗显示 `Key ID`（PK 开头）和 `Secret Key`（40 位左右）—— **Secret Key 只显示一次**，立刻复制到记事本

### Step 2 — 写 .env 文件

```bash
cp .env.example .env
```

用编辑器打开 `.env`，把两行占位符替换成真 key：
```
ALPACA_API_KEY=PK...你的 Key ID
ALPACA_SECRET_KEY=...你的 Secret Key
```
保存。`.env` 已在 `.gitignore` 里，不会被 commit。

### Step 3 — 自检

```bash
python check_setup.py
```

6 项检查全部 ✅ 才往下走。如果 "Alpaca API 认证" 那项 ❌ HTTP 401，多半是 key 复制漏字符或选错了 Live/Paper。

### Step 4 — 跑

```bash
python wheel_summary.py open        # 开盘摘要（只读）
python wheel_summary.py close       # 收盘摘要（只读）
python wheel_health_check.py        # 标的健康检查（只读）
python wheel_scanner.py             # 候选扫描（只读）
python wheel_once.py                # ⚠️ 会下 paper 单！
```

---

## ☁️ 路径 3：GitHub Actions（生产，自动定时跑）

仓库里的 workflows 已经配好（`.github/workflows/wheel.yml` 每 5 分钟，`wheel-summary.yml` 开/收盘各一次）。只需要把 key 配到仓库 Secrets。

### Step 1 — 配 Secrets

1. 浏览器打开 **`https://github.com/tonyatbellevue/ai-trading-wheel/settings/secrets/actions`**
2. 右上角 **New repository secret**
3. 加 `ALPACA_API_KEY` → 粘 Key ID
4. 再加 `ALPACA_SECRET_KEY` → 粘 Secret Key

### Step 2 — 手动触发一次验证

1. 打开 **`https://github.com/tonyatbellevue/ai-trading-wheel/actions/workflows/wheel-summary.yml`**
2. 右上角 **Run workflow** → Branch `main` → 绿色按钮 **Run workflow**
3. 等 1-2 分钟 → 点进那次运行 → 看到 ✅ 说明配好
4. 结果：仓库 Issues 里会出现 `[Open] ...` 通知

### Step 3 — 等它自动跑

之后就自动了。每个交易日：
- **每 5 分钟**：`wheel.yml` 跑一次 → 有新交易就发 Issue
- **开盘 9:30 ET**：`wheel-summary.yml` 跑一次 → 发 `[Open]` Issue
- **收盘 4:05 ET**：`wheel-summary.yml` 再跑一次 → 发 `[Close]` Issue

---

## 🛠️ 常见故障

### `ta` 包装不上 (`Failed building wheel for ta`)
`ta` 只被老的 EMA/RSI 模型路径用（`train_model.py` / `run_backtest.py`），**Wheel 策略不需要**。跳过它：

```bash
pip install alpaca-py pandas numpy loguru python-dotenv pydantic pytz requests apscheduler joblib flask
```

想装 `ta` 的话，先降级 setuptools：
```bash
pip install 'setuptools<68'
pip install ta
```

### `ValueError: You must supply a method of authentication`
没设 `ALPACA_API_KEY` 或 `ALPACA_SECRET_KEY`。跑 `python check_setup.py` 看哪项缺。

### HTTP 401 unauthorized
Key 是错的、过期了、或者你给的是 Live account key（我们要 Paper）。去 `https://app.alpaca.markets/paper/dashboard/overview` 重新生成。

### `ModuleNotFoundError: No module named 'alpaca'`
```bash
pip install alpaca-py
```

### Secrets 加了但 Actions 跑时还是 401
可能 secret 名字写错了。必须一字不差：`ALPACA_API_KEY` 和 `ALPACA_SECRET_KEY`。大小写、下划线都要对。

---

## 📂 跑法对照表

| 场景 | 命令 | 需要 key | 会下单吗 |
|------|------|---------|---------|
| 看 demo 样式 | `python wheel_demo.py open` | ❌ | ❌ |
| 本地查看状态 | `python wheel_summary.py open` | ✅ | ❌ |
| 本地健康检查 | `python wheel_health_check.py` | ✅ | ❌ |
| 本地候选扫描 | `python wheel_scanner.py` | ✅ | ❌ |
| 本地触发一次 Wheel | `python wheel_once.py` | ✅ | ⚠️ 可能（paper） |
| Actions 定时 Summary | Run workflow `wheel-summary.yml` | ✅（Secrets） | ❌ |
| Actions 定时 Wheel | Run workflow `wheel.yml` | ✅（Secrets） | ⚠️ 可能（paper） |

> **安全默认**：所有 workflow 都设了 `PAPER_TRADING=true`，用的是 paper endpoint。**不会动真金账户**。
