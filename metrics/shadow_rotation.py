"""Shadow Rotation — 每日记录"如果用 v3 轮换逻辑会怎样"

不真的交易，只记录虚拟决策+虚拟权益。每日运行一次（由 health_check 触发）。
4 周后可对比实盘账户 vs shadow 的表现差异。

记录内容：
  - 当日所有候选标的的 v3 评分
  - Top1 选择（即 shadow 策略本轮会持有的）
  - 虚拟账户权益（$100k 起始，跟踪 Top1 收益 + 假设权利金）
  - 如果 Top1 变化，记录"shadow 轮换"

数据文件：
  - metrics/data/shadow_state.json     当前虚拟账户状态
  - metrics/data/shadow_decisions.csv  每日决策日志
  - metrics/data/shadow_equity.csv     每日虚拟权益
  - metrics/data/shadow_rotations.csv  轮换历史
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings
from backtest.wheel_pricing import quote_option, find_strike_by_delta
from strategy.wheel_scoring import extract_features, score_symbol


DATA_DIR = Path(settings.BASE_DIR) / "metrics" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE      = DATA_DIR / "shadow_state.json"
DECISIONS_CSV   = DATA_DIR / "shadow_decisions.csv"
EQUITY_CSV      = DATA_DIR / "shadow_equity.csv"
ROTATIONS_CSV   = DATA_DIR / "shadow_rotations.csv"

# ── 候选池 ────────────────────────────────────────────────────────────
SHADOW_UNIVERSE = ["TSLA", "NVDA", "AAPL", "MSFT", "META", "AMZN", "GOOGL", "AMD"]

# ── 参数（v3 与回测一致）────────────────────────────────────────────────
INITIAL_CAPITAL = 100_000.0
TARGET_DELTA = -0.25
DTE = 3
CONTRACTS = 2
# 15% 阈值（保守）— 对应 settings.SWITCH_ON_IDLE_GO 的语义
ROTATION_THRESHOLD = getattr(settings, "SWITCH_ON_IDLE_GO", 0.15)


# ── 状态管理 ───────────────────────────────────────────────────────────

def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "initialized_at": datetime.now().isoformat(),
            "cash": INITIAL_CAPITAL,
            "equity": INITIAL_CAPITAL,
            "current_symbol": "TSLA",
            "shares": 0,
            "cost_basis": 0,
            "phase": "IDLE",
            "opt_strike": 0,
            "opt_expiry": None,
            "opt_premium": 0,
            "opt_qty": 0,
            "total_premium_collected": 0,
            "rotations_count": 0,
            "last_cycle_pnl": 0,
        }
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def _save_state(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s, indent=2, default=str), encoding="utf-8")


# ── 数据获取 ───────────────────────────────────────────────────────────

def _fetch_features() -> dict:
    """Pull the latest features for every symbol in SHADOW_UNIVERSE.

    Uses a single batched Alpaca request (8× faster than per-symbol loop)
    and delegates feature extraction to strategy.wheel_scoring.
    """
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import Adjustment

    client = StockHistoricalDataClient(
        api_key=settings.API_KEY, secret_key=settings.SECRET_KEY,
    )
    end = datetime.now() - timedelta(days=1)
    start = end - timedelta(days=180)

    out = {}
    try:
        req = StockBarsRequest(
            symbol_or_symbols=SHADOW_UNIVERSE,
            timeframe=TimeFrame.Day,
            start=start, end=end,
            adjustment=Adjustment.ALL,
        )
        resp = client.get_stock_bars(req)
        # resp.data is dict {symbol: [bars]}
        data = getattr(resp, "data", None) or resp
    except Exception as e:
        logger.warning(f"Shadow batch fetch failed: {e}")
        return out

    for sym in SHADOW_UNIVERSE:
        bars = data.get(sym) if hasattr(data, "get") else None
        if not bars:
            continue
        try:
            closes = [float(b.close) for b in bars]
            feat = extract_features(closes)
            if feat:
                out[sym] = feat
        except Exception as e:
            logger.debug(f"Shadow feature extract {sym} failed: {e}")
    return out


# Back-compat shim so older callers / tests keep working.
_score_v3 = score_symbol


# ── 每日核心逻辑 ───────────────────────────────────────────────────────

def daily_snapshot() -> dict:
    """每日快照 — 拉数据、评分、决策、更新虚拟账户、写日志"""
    today = date.today()
    state = _load_state()

    features = _fetch_features()
    if not features:
        logger.warning("Shadow: 无可用特征数据，跳过")
        return {"status": "no_data"}

    # 所有标的评分
    scores = {sym: _score_v3(feat) for sym, feat in features.items()}
    top_sym = max(scores, key=scores.get)
    top_score = scores[top_sym]
    cur_sym = state["current_symbol"]
    cur_score = scores.get(cur_sym, 0)
    cur_feat = features.get(cur_sym)

    # ── 更新虚拟账户：模拟简化 wheel 逻辑 ──
    rotated = False
    if state["phase"] == "IDLE" and cur_feat:
        # 1. 决策：是否换标的（仅 IDLE 可换）
        # last_pnl >= 0 允许（含首次运行），负数才锁死
        last_pnl = state.get("last_cycle_pnl", 0)
        if (last_pnl >= 0 and top_sym != cur_sym and cur_score > 0
                and top_score > cur_score * (1 + ROTATION_THRESHOLD)):
            _log_rotation(today, cur_sym, top_sym, cur_score, top_score)
            state["current_symbol"] = top_sym
            state["rotations_count"] += 1
            cur_sym = top_sym
            cur_feat = features[cur_sym]
            rotated = True

        # 2. 卖 Put（简化：假设 3 天到期，权利金立即收取，期末结算）
        S = cur_feat["price"]
        iv = cur_feat["iv"]
        T = DTE / 365
        try:
            strike = find_strike_by_delta(S, TARGET_DELTA, T, 0.045, iv, is_call=False)
            q = quote_option(S, strike, DTE, iv, is_call=False, r=0.045)
            premium = q.price
        except Exception:
            premium = 0

        if premium > 0 and state["cash"] >= strike * 100 * CONTRACTS:
            state["cash"] += premium * 100 * CONTRACTS
            state["total_premium_collected"] += premium * 100 * CONTRACTS
            state["phase"] = "SHORT_PUT"
            state["opt_strike"] = strike
            state["opt_expiry"] = (today + timedelta(days=DTE)).isoformat()
            state["opt_premium"] = premium
            state["opt_qty"] = CONTRACTS

    # 3. 到期结算
    elif state["phase"] == "SHORT_PUT" and state["opt_expiry"]:
        expiry = datetime.fromisoformat(state["opt_expiry"]).date()
        if today >= expiry and cur_feat:
            S = cur_feat["price"]
            strike = state["opt_strike"]
            qty = state["opt_qty"]
            if S < strike:
                # 被行权
                cost = strike * 100 * qty
                state["cash"] -= cost
                state["shares"] = 100 * qty
                state["cost_basis"] = strike - state["opt_premium"]
                state["phase"] = "LONG_STOCK"
                state["last_cycle_pnl"] = state["opt_premium"] * 100 * qty - (strike - S) * 100 * qty
            else:
                # 赚钱结束
                state["phase"] = "IDLE"
                state["last_cycle_pnl"] = state["opt_premium"] * 100 * qty
            state["opt_strike"] = 0
            state["opt_expiry"] = None
            state["opt_premium"] = 0
            state["opt_qty"] = 0

    # 4. LONG_STOCK: 卖 Covered Call
    elif state["phase"] == "LONG_STOCK" and cur_feat:
        S = cur_feat["price"]
        iv = cur_feat["iv"]
        T = DTE / 365
        try:
            strike = find_strike_by_delta(S, 0.25, T, 0.045, iv, is_call=True)
            strike = max(strike, state["cost_basis"])
            q = quote_option(S, strike, DTE, iv, is_call=True, r=0.045)
            premium = q.price
        except Exception:
            premium = 0

        if premium > 0:
            cc_qty = state["shares"] // 100
            state["cash"] += premium * 100 * cc_qty
            state["total_premium_collected"] += premium * 100 * cc_qty
            state["phase"] = "SHORT_CALL"
            state["opt_strike"] = strike
            state["opt_expiry"] = (today + timedelta(days=DTE)).isoformat()
            state["opt_premium"] = premium
            state["opt_qty"] = cc_qty

    # 5. SHORT_CALL 到期
    elif state["phase"] == "SHORT_CALL" and state["opt_expiry"]:
        expiry = datetime.fromisoformat(state["opt_expiry"]).date()
        if today >= expiry and cur_feat:
            S = cur_feat["price"]
            strike = state["opt_strike"]
            qty = state["opt_qty"]
            if S > strike:
                # 股票被收走
                state["cash"] += strike * 100 * qty
                stock_pnl = (strike - state["cost_basis"]) * 100 * qty
                state["shares"] -= 100 * qty
                state["last_cycle_pnl"] = state["opt_premium"] * 100 * qty + stock_pnl
                if state["shares"] <= 0:
                    state["cost_basis"] = 0
                    state["phase"] = "IDLE"
                else:
                    state["phase"] = "LONG_STOCK"
            else:
                state["phase"] = "LONG_STOCK"
                state["last_cycle_pnl"] = state["opt_premium"] * 100 * qty
            state["opt_strike"] = 0
            state["opt_expiry"] = None
            state["opt_premium"] = 0
            state["opt_qty"] = 0

    # ── 计算虚拟权益 ──
    S = features.get(state["current_symbol"], {}).get("price", 0)
    equity = state["cash"] + state["shares"] * S
    state["equity"] = round(equity, 2)

    # ── 写日志 ──
    _log_decision(today, state, scores, rotated)
    _log_equity(today, state, S)
    _save_state(state)

    return {
        "status": "ok",
        "date": today.isoformat(),
        "shadow_equity": state["equity"],
        "shadow_symbol": state["current_symbol"],
        "shadow_phase": state["phase"],
        "top_sym": top_sym,
        "top_score": top_score,
        "rotated": rotated,
        "rotations_total": state["rotations_count"],
        "premium_collected": state["total_premium_collected"],
        "scores": scores,
    }


# ── 日志写入 ───────────────────────────────────────────────────────────

def _log_decision(today: date, state: dict, scores: dict, rotated: bool) -> None:
    row = {
        "date": today.isoformat(),
        "symbol": state["current_symbol"],
        "phase": state["phase"],
        "equity": state["equity"],
        "cash": state["cash"],
        "shares": state["shares"],
        "rotated": "1" if rotated else "0",
        "rotations_total": state["rotations_count"],
        "premium_total": state["total_premium_collected"],
    }
    # 每个标的的评分展开
    for sym, sc in scores.items():
        row[f"score_{sym}"] = sc

    header_needed = not DECISIONS_CSV.exists()
    with open(DECISIONS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if header_needed:
            w.writeheader()
        w.writerow(row)


def _log_equity(today: date, state: dict, price: float) -> None:
    row = {
        "date": today.isoformat(),
        "shadow_equity": state["equity"],
        "cash": state["cash"],
        "symbol": state["current_symbol"],
        "phase": state["phase"],
        "spot_price": price,
    }
    header_needed = not EQUITY_CSV.exists()
    with open(EQUITY_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if header_needed:
            w.writeheader()
        w.writerow(row)


def _log_rotation(today: date, from_sym: str, to_sym: str,
                   from_score: float, to_score: float) -> None:
    row = {
        "date": today.isoformat(),
        "from_symbol": from_sym,
        "to_symbol": to_sym,
        "from_score": from_score,
        "to_score": to_score,
        "improvement": f"{(to_score - from_score) / from_score:.1%}" if from_score > 0 else "N/A",
    }
    header_needed = not ROTATIONS_CSV.exists()
    with open(ROTATIONS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if header_needed:
            w.writeheader()
        w.writerow(row)
    logger.info(f"[Shadow] 轮换登记: {from_sym} → {to_sym} (评分 {from_score:.1f} → {to_score:.1f})")


# ── 报告（供周报使用）──────────────────────────────────────────────────

def format_shadow_report() -> str:
    """生成 Shadow vs 实盘对比 Markdown"""
    if not STATE_FILE.exists() or not EQUITY_CSV.exists():
        return ""

    state = _load_state()
    started = datetime.fromisoformat(state["initialized_at"]).date()
    days = (date.today() - started).days

    # 实盘权益
    try:
        from core.alpaca_client import AlpacaClients
        real_equity = float(AlpacaClients.trading().get_account().equity)
    except Exception:
        real_equity = INITIAL_CAPITAL

    shadow_eq = state["equity"]
    real_ret = (real_equity - INITIAL_CAPITAL) / INITIAL_CAPITAL
    shadow_ret = (shadow_eq - INITIAL_CAPITAL) / INITIAL_CAPITAL
    diff = shadow_ret - real_ret

    lines = [
        "## 🧪 Shadow Rotation 对照（假设使用 v3 新逻辑）",
        "",
        f"_对照运行 {days} 天 · 起始 ${INITIAL_CAPITAL:,.0f}_",
        "",
        "| 指标 | 实盘 (旧逻辑) | Shadow (v3 新逻辑) | 差额 |",
        "|------|--------------|-------------------|------|",
        f"| 当前权益 | ${real_equity:,.2f} | ${shadow_eq:,.2f} | ${shadow_eq - real_equity:+,.2f} |",
        f"| 总收益率 | {real_ret:+.2%} | {shadow_ret:+.2%} | {diff:+.2%} |",
        f"| 当前标的 | {settings.WHEEL_SYMBOL} | {state['current_symbol']} | - |",
        f"| 轮换次数 | 0 | {state['rotations_count']} | - |",
        f"| 累计权利金 | - | ${state['total_premium_collected']:,.0f} | - |",
        "",
    ]

    if days < 7:
        lines.append("_样本期不足 7 天，结论不可靠_")
    elif days >= 28:
        if diff > 0.02:
            lines.append("✅ **Shadow 新逻辑领先 2%+，建议下月切换到新逻辑**")
        elif diff < -0.02:
            lines.append("❌ **Shadow 新逻辑落后 2%+，暂不采纳**")
        else:
            lines.append("➖ 两者差异 < 2%，继续观察")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    result = daily_snapshot()
    print(json.dumps(result, indent=2, default=str))
    print()
    print(format_shadow_report())
