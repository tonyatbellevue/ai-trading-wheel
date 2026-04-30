# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⏰ User timezone — ALWAYS double-stamp times

The user lives in **Singapore (SGT, UTC+8)**. The market is **US Eastern (ET, UTC-4 with DST)**. GitHub Actions cron runs in **UTC**. These three are 12+ hours apart, and ambiguous timestamps have already caused real confusion.

**Rules when writing or speaking times:**

1. **Always tag the timezone explicitly.** Never say "21:30" alone — always "21:30 SGT" or "09:30 ET".
2. **For schedules and emails, show both SGT and ET.** Example: "OPEN email at 09:31 ET = 21:31 SGT".
3. **For "today" / "tomorrow", clarify which calendar.** US trading day 4/30 spans SGT 4/30 evening through SGT 5/1 morning — say "美东 4/30" or "ET 4/30" to disambiguate.
4. **Cron strings stay in UTC** (that's what GHA reads), but include the SGT translation in the comment.

**Quick reference:**
```
SGT = UTC + 8     SGT = ET + 12 (DST)  SGT = ET + 13 (non-DST)
US market open  09:31 ET = 13:31 UTC = 21:31 SGT (user's evening)
US market close 16:05 ET = 20:05 UTC = 04:05 SGT next day (user's pre-dawn)
```

The user's "today" is mostly SGT, so when they wake up they're checking the *previous* US trading day's close email and considering an action for *that same evening's* US session.

## 🎯 Scope — Alpaca paper account ONLY

**Never discuss the user's real-money brokerage account (Merrill Lynch / ML).** All conversations, recommendations, status checks, and analysis are scoped to the Alpaca paper trading account that the wheel bot operates on.

If the user asks about positions, P&L, options trades, or "the account," default to Alpaca paper. Do not bring up ML holdings (PLTR, QQQ, UNH, cost basis, LTCG, real-money covered calls) unless the user explicitly opens that thread, and even then keep it minimal.

The bot's universe is the 32 tickers in `wheel_scanner.WHEEL_UNIVERSE`. The bot's account is the one returned by `AlpacaClients.trading()`. That's it.

## What this repo is

A paper-trading Wheel options bot running against Alpaca. The strategy sells cash-secured puts, takes assignment when they go ITM, sells covered calls, and rotates the underlying symbol based on health + evaluator score. Separately, there's an older EMA/RSI + RandomForest stock-trading entry point that predates the wheel work.

## Common commands

```bash
# Interactive entry (17 numbered operations: account / live / backtests / emails)
python trading.py

# Single wheel cycle (idempotent; what GitHub Actions calls every 5 min)
python wheel_once.py

# Email dashboard to bellevuetony@hotmail.com
python email_summary.py open        # market-open summary
python email_summary.py close       # market-close summary (+ weekly review on Fri)

# System health — 7 checks + shadow-rotation snapshot
python scripts/health_check.py --no-email

# Backtests
python run_wheel_backtest.py --symbol TSLA --start 2023-01-01 --end 2025-12-31
python run_wheel_backtest_multi.py              # 5-symbol × 12 variants
python backtest/rotation_backtest.py            # v3-scoring rotation, 24 months
python run_backtest.py --no-ml                  # legacy EMA+RSI strategy

# Universe validation (liquidity + option chain check)
python scripts/validate_universe.py --add NEW_SYMBOL

# Long-running (Ctrl+C to stop)
python main.py                  # EMA+RSI+ML live bot
python run_wheel_csco.py        # continuous wheel loop (cron-less)
python wheel_server.py          # Flask dashboard at :5000
```

No linter / test framework configured; syntax-check everything with:
```bash
python -c "import ast, os; [ast.parse(open(os.path.join(r,f), encoding='utf-8').read()) for r,_,fs in os.walk('.') if '.git' not in r and '__pycache__' not in r for f in fs if f.endswith('.py')]"
```

## Worktree-aware .env loading

This repo lives in a git worktree under `.claude/worktrees/competent-jones/`. The real `.env` is in the **main project root** at `C:\Users\belle\OneDrive\Desktop\AI Trading\.env`. `config/settings.py:_resolve_env_path()` detects the worktree by reading the `.git` file and loads `.env` from the main project automatically — **do not duplicate the `.env` in the worktree**, it will be ignored. Override via `AI_TRADING_ENV_PATH` env var if needed.

## Architecture

### Two independent strategies live side by side

1. **Wheel options (primary, actively developed)** — driven by `wheel_once.py` → `strategy/wheel_strategy.py:WheelStrategy.run_cycle()`.
2. **EMA+RSI+ML stock trading (legacy)** — driven by `main.py`, uses `strategy/ema_rsi_strategy.py` + `models/rf_classifier.py` over an event bus. Still works but no longer the focus.

### Wheel state machine (`strategy/wheel_strategy.py`)

```
IDLE → sell CSP → SHORT_PUT → (expire OTM → IDLE) | (assigned → LONG_STOCK → sell CC → SHORT_CALL → expire OTM → LONG_STOCK | assigned → IDLE)
```

Phase is **derived from live Alpaca positions + open orders**, not stored. Read `get_phase()` — there's no persistent state machine to desync.

### Per-cycle decision flow in `run_cycle()`

1. `get_phase()` from live Alpaca
2. `evaluate_and_maybe_plan()` → may register a rotation plan in `wheel_symbol.json`
3. `maybe_switch()` → if plan fires **and** we're IDLE, swap `settings.WHEEL_SYMBOL` and **return early** (next cron tick uses the new symbol)
4. Phase-specific action:
   - IDLE: run 3 filters (earnings / MA50 / realized-vol) → `select_put()` → `kelly_contracts()` → `check_buying_power_sufficient()` → `sell_to_open()`
   - LONG_STOCK: earnings filter → `select_call(cost_basis)` → sell CC (1 per 100 shares)
   - SHORT_PUT / SHORT_CALL: no-op, wait for expiry

### Position sizing is resource-driven, NOT Kelly

Despite the name, `strategy/wheel_filters.py:kelly_contracts()` is **not** Kelly anymore. The traditional Kelly formula `f* = p - q/b` was dead code for 3-5 DTE puts (`b = premium/strike ≈ 0.01 << q/p ≈ 0.18` → always clamped to 0) and the old `max(1, ...)` tail masked it. The current function ignores the Kelly parameters and computes:

```
max_contracts = min(
    (buying_power - equity × CASH_BUFFER_PCT) / (strike × 100),           # layer 1: BP minus buffer
    (equity × MAX_SINGLE_POSITION_PCT) / (strike × 100),                  # layer 2: single-position cap
    (equity × MAX_TOTAL_EXPOSURE_PCT - existing_put_collateral) / (strike × 100),  # layer 3: aggregate cap
)
# then a worst-case assertion: (existing + new_collateral + buffer) <= cash
```

Defaults: `CASH_BUFFER_PCT=0.10`, `MAX_SINGLE_POSITION_PCT=0.70`, `MAX_TOTAL_EXPOSURE_PCT=0.90` (in `config/settings.py`). `check_buying_power_sufficient()` is a separate pre-order hard gate in case Kelly returns a non-zero count through a bug.

**Do not resurrect Kelly math here** — it doesn't apply to cash-secured short puts.

### Rotation / "should I switch symbol" logic (`strategy/wheel_evaluator.py`)

Two independent mechanisms must both agree before a swap happens:

1. **Score comparison**: `_find_better_alternative()` → `wheel_scanner.scan_wheel_alternatives()` (returns a **dict** `{baseline, better, all}`, not a list — a silent bug fix from before). A swap only triggers at +10% improvement when CAUTION, +15% when GO+IDLE.
2. **"Last cycle was profitable"** gate: `_last_cycle_was_profitable()` checks three sources in order — `metrics/trade_journal.csv` last exit, Alpaca closed orders over 14 days, then daily equity delta. If the last cycle lost money we **stay put** until it recovers; this is intentional, don't "optimize" it out.

There are **two scoring systems** in the repo (historical accident):
- `wheel_scanner.py` — original scoring, still used by live `wheel_evaluator`
- `backtest/rotation_backtest.py` + `metrics/shadow_rotation.py` — "v3" stability-weighted scoring (RV 30% + 30d DD 15% + IV sweet zone 15% + others)

v3 is backtest-validated (+11.7% annualized vs -2.3% Fixed-TSLA, MaxDD 9% vs 46% over 24 months) and runs as a **shadow** account daily. Promotion to live depends on 4 weeks of shadow-vs-real comparison.

### Universe (`wheel_scanner.py`)

Hand-picked 32 tech-heavy tickers (Mag 7, semis, photonics MRVL/LITE/COHR/IPGP, SMCI, XYL, DRAM ETF, defensives COST/UNH). When adding a symbol, validate with `scripts/validate_universe.py --add SYM` — it checks volume + option chain + 25-delta bid-ask spread.

### Execution / automation

- **GitHub Actions (primary cron)** — `.github/workflows/wheel.yml` runs `wheel_once.py` every 5 min on weekdays 13:30–20:00 UTC. `.github/workflows/wheel-summary.yml` sends open/close emails. Secrets: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `EMAIL_SMTP_SERVER`, `EMAIL_SMTP_PORT`, `EMAIL_SENDER`, `EMAIL_PASSWORD`, `EMAIL_RECIPIENT`. Cron only fires on the default branch (`master`), so feature-branch workflow changes must be merged to take effect.
- **Local scheduled-tasks** — Claude Code's MCP scheduler runs `email_summary.py` as a redundant secondary trigger (only fires while Claude Code is running on the user's machine).
- **Both fire** — expect duplicate emails until one is disabled; GitHub Actions is the authoritative source.

### Email pipeline

`email_summary.py` → `wheel_summary.build_summary(event)` produces markdown → `utils/dashboard_renderer.render_dashboard(md)` converts to themed HTML → `utils/emailer.send_email()` sends via Gmail SMTP with 3-try exponential backoff. Failed mails land in `reports/failed_emails/*.json` for later `retry_failed_emails()` replay.

### Observability layer (`metrics/`)

- `baseline_tracker.py` — daily equity snapshot (`daily_snapshot.csv`) + week-over-week report
- `trade_journal.py` — 24-field per-decision log (entry / skip / exit), used by weekly reports and by `_last_cycle_was_profitable()`
- `weekly_review.py` — Friday-close deep report (best/worst trades, filter contribution, 30-day plan progress)
- `shadow_rotation.py` — virtual v3 wheel account; `daily_snapshot()` is called from `scripts/health_check.py` every morning

All under `metrics/data/` is **gitignored** (contains account equity).

### Useful docs

- `docs/WHEEL_OPERATION_MANUAL.md` — decision-flow spec with exact thresholds (the human-readable source of truth for how run_cycle behaves)
- `docs/STRATEGY_REPORT_AND_30D_PLAN.md` — 30-day improvement roadmap with rollback criteria
- `docs/week1_review.md`, `docs/week2_review.md` — what was fixed and why (roll mechanism, evaluator bug, sizer rewrite)

## Gotchas

- **Alpaca paper-trading API key has no grace period**: regenerating in the dashboard invalidates the old key instantly. When tests start returning `{"message":"unauthorized."}`, the key was rotated. Update both `.env` (main project) and GitHub Secrets via `gh secret set ALPACA_API_KEY`.
- **Alpaca free tier can't query the last 15 min of data**; backtests and scanners set `end = now - 2 days` as a safe margin.
- **NVDA pre-split prices**: set `adjustment=Adjustment.ALL` in `StockBarsRequest` or the 2024-06-10 10:1 split will make NVDA look like it crashed 70%.
- **GBK console on Windows**: `logger` (loguru) emoji output garbles; use `sys.stdout.reconfigure(encoding="utf-8")` at script top.
- **Option chain Greeks for 0DTE are often `None`** from Alpaca. `zero_dte_recommender.py` falls back to Black-Scholes via `backtest/wheel_pricing.py:bs_greeks` using realized vol as an IV proxy.
- **`wheel_symbol.json` is auto-committed** by the wheel.yml workflow when the evaluator rotates symbols — expect `chore: auto-update wheel_symbol.json [skip ci]` commits on master.
