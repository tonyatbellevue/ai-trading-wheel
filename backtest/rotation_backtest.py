"""数据驱动的多标的轮换回测

模拟 wheel_evaluator 的实盘逻辑：
  - IDLE 时（option 周期赚钱结束）→ 比较所有标的评分，切到 Top1（如评分高 ≥ 15%）
  - 持仓中 → 不切换
  - Put 被行权 → 进入 LONG_STOCK 阶段，必须卖 Call 直到摆脱
  - "赚钱周期结束"才允许切换（被行权周期保持）

对比基线：固定单一标的（如 TSLA）的传统 wheel 策略。
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, datetime, timedelta
from dataclasses import dataclass, field
from math import log, sqrt
from statistics import mean, stdev
from typing import Optional

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment

from config import settings
from backtest.wheel_pricing import quote_option, find_strike_by_delta
from strategy.wheel_scoring import (
    extract_features_from_df as _stock_features,
    realized_vol as _realized_vol,
    score_symbol as _score_symbol,
)


# ── 状态机 ────────────────────────────────────────────────────────────

@dataclass
class WheelPos:
    """单标的的 wheel 状态"""
    symbol: str = ""
    phase: str = "IDLE"            # IDLE | SHORT_PUT | LONG_STOCK | SHORT_CALL
    shares: int = 0
    cost_basis: float = 0.0
    opt_strike: float = 0.0
    opt_expiry_idx: int = -1       # 期权到期对应的 bar index
    opt_premium: float = 0.0
    opt_qty: int = 0


@dataclass
class Trade:
    open_date: date
    close_date: Optional[date]
    symbol: str
    option_type: str
    strike: float
    qty: int
    entry_premium: float
    pnl: float = 0.0
    outcome: str = ""              # expire_otm | assigned | rolled


# ── 主回测 ────────────────────────────────────────────────────────────

def run_rotation_backtest(
    bars_dict: dict[str, pd.DataFrame],   # {symbol: daily_df}
    initial_capital: float = 100_000.0,
    target_delta: float = -0.25,
    dte: int = 3,
    contracts: int = 2,
    rotation_enabled: bool = True,
    fixed_symbol: Optional[str] = None,
    rotation_threshold: float = 0.15,
    iv_premium: float = 1.15,
    r: float = 0.045,
) -> dict:
    """跑轮换 wheel 回测

    Args:
        bars_dict: {symbol: 日线 DataFrame, index=date, columns=open/close/...}
        rotation_enabled: True = 数据驱动轮换, False = 固定 fixed_symbol
        fixed_symbol: 当 rotation_enabled=False 时使用
        rotation_threshold: 切换阈值（Top1 评分 ≥ baseline × (1+threshold)）
    """
    # 构建统一时间轴（所有标的的交集）
    common_dates = None
    for sym, df in bars_dict.items():
        dates = set(df.index)
        common_dates = dates if common_dates is None else common_dates & dates
    common_dates = sorted(common_dates)

    if len(common_dates) < 60:
        raise ValueError(f"数据太少: {len(common_dates)} 天")

    # 索引映射：每个 symbol 的 date → bar index
    sym_idx_map = {}
    for sym, df in bars_dict.items():
        sym_idx_map[sym] = {d: i for i, d in enumerate(df.index)}

    cash = initial_capital
    pos = WheelPos(symbol=fixed_symbol if not rotation_enabled else "TSLA", phase="IDLE")
    trades: list[Trade] = []
    rotations = []     # [(date, from_sym, to_sym, reason)]
    equity_curve = []

    # 统计
    last_cycle_pnl = 0.0   # 用于"赚钱才换"判断

    for day_idx, today in enumerate(common_dates):
        if day_idx < 50:
            # 预热期，不交易
            equity_curve.append((today, cash))
            continue

        # 当前标的的当日 bar
        cur_sym = pos.symbol
        cur_df = bars_dict[cur_sym]
        cur_local_idx = sym_idx_map[cur_sym][today]
        S = float(cur_df.iloc[cur_local_idx]["close"])

        # ── 期权到期结算 ──
        if pos.phase in ("SHORT_PUT", "SHORT_CALL") and day_idx >= pos.opt_expiry_idx:
            tr = trades[-1]
            if pos.phase == "SHORT_PUT":
                if S < pos.opt_strike:
                    # 被行权
                    cost = pos.opt_strike * 100 * pos.opt_qty
                    cash -= cost
                    pos.shares = 100 * pos.opt_qty
                    pos.cost_basis = pos.opt_strike - pos.opt_premium
                    tr.outcome = "assigned"
                    tr.pnl = pos.opt_premium * 100 * pos.opt_qty
                    pos.phase = "LONG_STOCK"
                    last_cycle_pnl = tr.pnl - (pos.opt_strike - S) * 100 * pos.opt_qty
                else:
                    # OTM 到期，收钱走人
                    tr.outcome = "expire_otm"
                    tr.pnl = pos.opt_premium * 100 * pos.opt_qty
                    last_cycle_pnl = tr.pnl
                    pos.phase = "IDLE"
            else:  # SHORT_CALL
                if S > pos.opt_strike:
                    # 被收走股票
                    proceeds = pos.opt_strike * 100 * pos.opt_qty
                    cash += proceeds
                    stock_pnl = (pos.opt_strike - pos.cost_basis) * 100 * pos.opt_qty
                    pos.shares -= 100 * pos.opt_qty
                    tr.outcome = "assigned"
                    tr.pnl = pos.opt_premium * 100 * pos.opt_qty + stock_pnl
                    last_cycle_pnl = tr.pnl
                    if pos.shares <= 0:
                        pos.cost_basis = 0
                        pos.phase = "IDLE"
                    else:
                        pos.phase = "LONG_STOCK"
                else:
                    tr.outcome = "expire_otm"
                    tr.pnl = pos.opt_premium * 100 * pos.opt_qty
                    last_cycle_pnl = tr.pnl
                    pos.phase = "LONG_STOCK"
            tr.close_date = today

        # ── IDLE 时：评估是否换标的 ──
        if pos.phase == "IDLE" and rotation_enabled:
            # 只在"上一周期赚钱"时才换
            if last_cycle_pnl > 0 or len(trades) == 0:
                # 算所有标的评分
                scores = {}
                for sym, df in bars_dict.items():
                    if today not in sym_idx_map[sym]:
                        continue
                    local_i = sym_idx_map[sym][today]
                    feats = _stock_features(df, local_i)
                    scores[sym] = _score_symbol(feats, dte=dte, target_delta=target_delta, r=r)

                if scores:
                    cur_score = scores.get(cur_sym, 0)
                    top_sym = max(scores, key=scores.get)
                    top_score = scores[top_sym]
                    if (top_sym != cur_sym and cur_score > 0
                            and top_score > cur_score * (1 + rotation_threshold)):
                        rotations.append((today, cur_sym, top_sym,
                                          f"score {cur_score:.1f}→{top_score:.1f}"))
                        pos.symbol = top_sym
                        cur_sym = top_sym
                        cur_df = bars_dict[cur_sym]
                        cur_local_idx = sym_idx_map[cur_sym][today]
                        S = float(cur_df.iloc[cur_local_idx]["close"])

        # ── 开新仓 ──
        if pos.phase == "IDLE":
            # 卖 Put
            closes_recent = [float(cur_df.iloc[i]["close"])
                             for i in range(max(0, cur_local_idx-20), cur_local_idx)]
            if len(closes_recent) < 5:
                equity_curve.append((today, cash + pos.shares * S))
                continue
            iv = _realized_vol(closes_recent) * iv_premium
            T = dte / 365
            try:
                strike = find_strike_by_delta(S, target_delta, T, r, iv, is_call=False)
                q = quote_option(S, strike, dte, iv, is_call=False, r=r)
                premium = q.price
            except Exception:
                premium = 0

            if premium > 0:
                # 现金检查
                required = strike * 100 * contracts
                if required > cash:
                    actual_contracts = max(int(cash // (strike * 100)), 0)
                else:
                    actual_contracts = contracts
                if actual_contracts > 0:
                    cash += premium * 100 * actual_contracts
                    expiry_idx = day_idx + dte
                    if expiry_idx >= len(common_dates):
                        expiry_idx = len(common_dates) - 1
                    pos.phase = "SHORT_PUT"
                    pos.opt_strike = strike
                    pos.opt_expiry_idx = expiry_idx
                    pos.opt_premium = premium
                    pos.opt_qty = actual_contracts
                    trades.append(Trade(
                        open_date=today, close_date=None,
                        symbol=cur_sym, option_type="P",
                        strike=strike, qty=actual_contracts,
                        entry_premium=premium,
                    ))

        elif pos.phase == "LONG_STOCK":
            # 卖 Call（必须 strike >= cost_basis）
            closes_recent = [float(cur_df.iloc[i]["close"])
                             for i in range(max(0, cur_local_idx-20), cur_local_idx)]
            if len(closes_recent) < 5:
                equity_curve.append((today, cash + pos.shares * S))
                continue
            iv = _realized_vol(closes_recent) * iv_premium
            T = dte / 365
            try:
                strike = find_strike_by_delta(S, abs(target_delta), T, r, iv, is_call=True)
                # 不锁亏
                strike = max(strike, pos.cost_basis)
                q = quote_option(S, strike, dte, iv, is_call=True, r=r)
                premium = q.price
            except Exception:
                premium = 0

            if premium > 0:
                cc_contracts = pos.shares // 100
                cash += premium * 100 * cc_contracts
                expiry_idx = day_idx + dte
                if expiry_idx >= len(common_dates):
                    expiry_idx = len(common_dates) - 1
                pos.phase = "SHORT_CALL"
                pos.opt_strike = strike
                pos.opt_expiry_idx = expiry_idx
                pos.opt_premium = premium
                pos.opt_qty = cc_contracts
                trades.append(Trade(
                    open_date=today, close_date=None,
                    symbol=cur_sym, option_type="C",
                    strike=strike, qty=cc_contracts,
                    entry_premium=premium,
                ))

        # ── 权益快照 ──
        equity = cash + pos.shares * S
        equity_curve.append((today, equity))

    # ── 最终清仓 ──
    if pos.shares > 0:
        last_S = float(bars_dict[pos.symbol].iloc[-1]["close"])
        cash += pos.shares * last_S

    # 统计
    final_eq = cash
    total_ret = (final_eq - initial_capital) / initial_capital
    days = (common_dates[-1] - common_dates[0]).days
    years = max(days / 365.25, 0.01)
    ann_ret = (final_eq / initial_capital) ** (1 / years) - 1

    closed_trades = [t for t in trades if t.outcome]
    num_trades = len(closed_trades)
    num_wins = sum(1 for t in closed_trades if t.outcome == "expire_otm")
    num_assigned = sum(1 for t in closed_trades if t.outcome == "assigned")
    win_rate = num_wins / num_trades if num_trades else 0
    total_premium = sum(t.entry_premium * 100 * t.qty for t in trades)

    # MaxDD
    peak = -float("inf")
    max_dd = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Sharpe
    eqs = [e for _, e in equity_curve]
    if len(eqs) > 2:
        rets = [(eqs[j] - eqs[j-1]) / eqs[j-1] for j in range(1, len(eqs))]
        m = sum(rets) / len(rets)
        v = stdev(rets) if len(rets) > 1 else 0
        sharpe = (m / v * sqrt(252)) if v > 0 else 0
    else:
        sharpe = 0

    return {
        "name": "Rotation" if rotation_enabled else f"Fixed-{fixed_symbol}",
        "initial_capital": initial_capital,
        "final_equity": final_eq,
        "total_return": total_ret,
        "annualized_return": ann_ret,
        "num_trades": num_trades,
        "num_wins": num_wins,
        "num_assigned": num_assigned,
        "win_rate": win_rate,
        "total_premium": total_premium,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "rotations": rotations,
        "trades": closed_trades,
    }


# ── 主程序 ────────────────────────────────────────────────────────────

def fetch_bars(symbols: list[str], days: int = 200) -> dict[str, pd.DataFrame]:
    client = StockHistoricalDataClient(
        api_key=settings.API_KEY, secret_key=settings.SECRET_KEY,
    )
    # Alpaca free 层不能查最近 15 min 数据，end 设为前天保险
    end = datetime.now() - timedelta(days=2)
    start = end - timedelta(days=days)
    out = {}
    for sym in symbols:
        try:
            req = StockBarsRequest(
                symbol_or_symbols=sym, timeframe=TimeFrame.Day,
                start=start, end=end,
                adjustment=Adjustment.ALL,   # 含分拆+股息调整
            )
            resp = client.get_stock_bars(req)
            bars = resp[sym]
            df = pd.DataFrame([{
                "date": b.timestamp.date(),
                "open": float(b.open), "high": float(b.high),
                "low": float(b.low), "close": float(b.close),
                "volume": float(b.volume),
            } for b in bars]).set_index("date").sort_index()
            out[sym] = df
            print(f"  {sym}: {len(df)} bars (${df['close'].min():.2f} - ${df['close'].max():.2f})")
        except Exception as e:
            print(f"  {sym}: FAILED ({e})")
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    # 24 个月 = 约 504 交易日，拉 800 天日历日保险
    UNIVERSE = ["TSLA", "NVDA", "AAPL", "MSFT", "META", "AMZN", "GOOGL", "AMD"]
    print(f"# 拉取 {len(UNIVERSE)} 标的最近 24 个月日线...\n")
    bars = fetch_bars(UNIVERSE, days=800)
    if not bars:
        print("没有数据，退出")
        return

    print(f"\n# 跑回测对比...\n")

    # 跑 5 个变体
    results = []

    # 1. 固定 TSLA（baseline）
    print("[1/5] 固定 TSLA wheel...")
    r = run_rotation_backtest(
        bars, rotation_enabled=False, fixed_symbol="TSLA",
    )
    results.append(r)

    # 2. 固定 MSFT
    print("[2/5] 固定 MSFT wheel...")
    r = run_rotation_backtest(
        bars, rotation_enabled=False, fixed_symbol="MSFT",
    )
    results.append(r)

    # 3. 固定 NVDA
    print("[3/5] 固定 NVDA wheel...")
    r = run_rotation_backtest(
        bars, rotation_enabled=False, fixed_symbol="NVDA",
    )
    results.append(r)

    # 4. 数据驱动轮换（15% 阈值，保守）
    print("[4/6] 数据驱动轮换 (15% 阈值)...")
    r = run_rotation_backtest(
        bars, rotation_enabled=True, rotation_threshold=0.15,
    )
    r["name"] = "Rotation-15%"
    results.append(r)

    # 5. 数据驱动轮换（10% 阈值，平衡）
    print("[5/6] 数据驱动轮换 (10% 阈值)...")
    r = run_rotation_backtest(
        bars, rotation_enabled=True, rotation_threshold=0.10,
    )
    r["name"] = "Rotation-10%"
    results.append(r)

    # 6. 数据驱动轮换（5% 阈值，激进）
    print("[6/6] 数据驱动轮换 (5% 阈值)...")
    r = run_rotation_backtest(
        bars, rotation_enabled=True, rotation_threshold=0.05,
    )
    r["name"] = "Rotation-5%"
    results.append(r)

    # 报告
    print("\n" + "=" * 70)
    print("# 6 个月轮换 vs 固定 标的回测")
    print("=" * 70)
    print()
    print(f"标的池: {', '.join(UNIVERSE)}")
    common_count = len(set.intersection(*(set(df.index) for df in bars.values())))
    print(f"共同交易日: {common_count} 天")
    print()
    print("| 策略 | 总收益 | 年化 | 胜率 | 交易数 | 行权 | MaxDD | Sharpe | 轮换次数 |")
    print("|------|--------|------|------|--------|------|-------|--------|---------|")
    for r in results:
        rot_count = len(r.get("rotations", []))
        print(f"| **{r['name']}** | {r['total_return']:+.2%} | {r['annualized_return']:+.2%} | "
              f"{r['win_rate']:.0%} | {r['num_trades']} | {r['num_assigned']} | "
              f"{r['max_drawdown']:.1%} | {r['sharpe']:+.2f} | {rot_count} |")

    # 每个轮换变体的详细历史
    for r in results:
        if "Rotation" not in r["name"]:
            continue
        print(f"\n## {r['name']} 轮换历史（共 {len(r.get('rotations', []))} 次）\n")
        for d, frm, to, reason in r.get("rotations", [])[:30]:
            print(f"- {d}: **{frm}** → **{to}** ({reason})")
        if not r.get("rotations"):
            print("_无轮换发生_")

    # 验证数据合理性
    print("\n## 数据合理性验证\n")
    for sym, df in bars.items():
        first_close = float(df.iloc[0]["close"])
        last_close = float(df.iloc[-1]["close"])
        period_ret = (last_close - first_close) / first_close
        print(f"  {sym}: ${first_close:.2f} → ${last_close:.2f} ({period_ret:+.1%})")

    # 写报告
    import os
    os.makedirs("reports", exist_ok=True)
    out = f"reports/rotation_backtest_24mo_v3.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# 6 个月轮换 wheel 回测\n\n")
        f.write(f"**生成时间**: {datetime.now().isoformat()}\n\n")
        f.write(f"**标的池**: {', '.join(UNIVERSE)}\n\n")
        f.write(f"**共同交易日**: {common_count}\n\n")
        f.write("| 策略 | 总收益 | 年化 | 胜率 | 交易数 | 行权 | MaxDD | Sharpe | 轮换 |\n")
        f.write("|------|--------|------|------|--------|------|-------|--------|------|\n")
        for r in results:
            rot_count = len(r.get("rotations", []))
            f.write(f"| {r['name']} | {r['total_return']:+.2%} | {r['annualized_return']:+.2%} | "
                    f"{r['win_rate']:.0%} | {r['num_trades']} | {r['num_assigned']} | "
                    f"{r['max_drawdown']:.1%} | {r['sharpe']:+.2f} | {rot_count} |\n")
        for r in results:
            if "Rotation" not in r["name"]:
                continue
            f.write(f"\n## {r['name']} 轮换详情（{len(r.get('rotations', []))} 次）\n\n")
            for d, frm, to, reason in r.get("rotations", []):
                f.write(f"- {d}: {frm} → {to} ({reason})\n")
    print(f"\n报告已保存: {out}")


if __name__ == "__main__":
    main()
