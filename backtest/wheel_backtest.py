"""Wheel 策略回测引擎 — 基于历史股价 + BS 定价

模型假设：
- 每周一/三/五开盘卖出 Put 或 Call（根据当前阶段）
- 期权 2-4 DTE
- IV ≈ 20日年化实现波动率 × iv_premium
- 到期时按收盘价判定 ITM/OTM 行权
- 不模拟盘中滚动价格，仅模拟开仓/到期两点
"""
from __future__ import annotations
import math
from math import sqrt, log
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import stdev
from typing import Optional

import pandas as pd

from backtest.wheel_pricing import quote_option, find_strike_by_delta, bs_greeks
from backtest.wheel_variants import WheelConfig


@dataclass
class Trade:
    open_date: date
    close_date: Optional[date]
    option_type: str          # "P" or "C"
    strike: float
    dte: int
    iv: float
    premium_per_share: float
    qty_contracts: int
    outcome: str = ""         # "expire_otm" | "assigned" | "rolled" | "profit_closed"
    pnl: float = 0.0          # 该单独期权交易 P&L（不含股票 P&L）


@dataclass
class WheelState:
    cash: float
    shares: int = 0                 # 0 或 100×合约数（被行权后）
    cost_basis: float = 0.0         # 股票成本基础（被put行权后）
    current_trade: Optional[Trade] = None


@dataclass
class BacktestResult:
    config_name: str
    initial_capital: float
    final_equity: float
    total_return: float
    annualized_return: float
    num_trades: int
    num_wins: int                   # 期权 OTM 到期或提前盈利平仓
    num_assigned: int               # 被行权
    num_rolled: int
    win_rate: float
    total_premium: float
    equity_curve: list = field(default_factory=list)  # [(date, equity)]
    max_drawdown: float = 0.0
    sharpe: float = 0.0
    trades: list = field(default_factory=list)


def _realized_vol(closes: list[float]) -> float:
    """20日年化实现波动率"""
    if len(closes) < 2:
        return 0.30
    rets = [log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
    if len(rets) < 2:
        return 0.30
    return stdev(rets) * sqrt(252)


def _resolve_delta(cfg: WheelConfig, iv: float) -> Optional[float]:
    """根据配置计算本次开仓的目标 Delta。返回 None 表示跳过。"""
    if cfg.use_iv_dynamic_delta:
        if cfg.low_iv_skip and iv < cfg.iv_low:
            return None
        if iv > cfg.iv_high:
            return cfg.target_delta * 0.6   # 更远 OTM
        if iv < cfg.iv_low:
            return cfg.target_delta          # 保持不变
    return cfg.target_delta


def _size_contracts(cfg: WheelConfig, cash: float, strike: float) -> int:
    """计算卖出合约数"""
    if cfg.use_kelly_sizing:
        # Kelly: f* = p - q/b, 其中 b = premium/strike（收益风险比）
        # 我们用估计胜率简化为固定百分比资金
        p = cfg.win_rate_est
        q = 1 - p
        b = 0.03       # 经验 risk/reward
        kelly = max(p - q / b, 0) * cfg.kelly_fraction
        allowed_cash = cash * kelly
        max_contracts = int(allowed_cash // (strike * 100))
        return max(1, min(max_contracts, cfg.contracts * 2))  # 至少1，最多翻倍
    return cfg.contracts


def _next_expiry_dte(df: pd.DataFrame, idx: int, cfg: WheelConfig) -> tuple[int, int]:
    """从当前 bar 往后找 2-4 DTE 的到期日所在 bar 索引"""
    for offset in range(cfg.min_dte, cfg.max_dte + 1):
        if idx + offset < len(df):
            return offset, idx + offset
    return 0, -1


def _try_roll_put(df: pd.DataFrame, idx: int, old_trade: Trade,
                   state: WheelState, cfg: WheelConfig, S: float,
                   iv: float, r: float) -> Optional[dict]:
    """尝试把 ITM Put 滚到下周更低 strike

    机制：
      1. 平掉当前 Put: BTC 成本 ≈ 内在价值 (max(strike - S, 0))
      2. 卖新 Put: strike_new = min(old_strike, S) × (1 - roll_down_pct)
      3. 必须满足: 新权利金 ≥ BTC 成本 × 0.5 才滚（否则白滚）
      4. 必须满足: 现金能 cover 新 strike × 100 × qty

    Returns:
        {"new_trade": Trade, "btc_cost": float} 滚单成功
        None 不滚（让原 trade 正常被行权）
    """
    # BTC 成本（内在价值）
    intrinsic = max(old_trade.strike - S, 0)
    btc_cost = intrinsic * 100 * old_trade.qty_contracts

    # 找下一个到期
    new_dte, new_idx = _next_expiry_dte(df, idx, cfg)
    if new_idx < 0:
        return None  # 数据不够，不能滚

    # 新 strike：在 min(原strike, 当前价) 基础上再下移 roll_down_pct
    base_strike = min(old_trade.strike, S)
    new_strike = base_strike * (1 - cfg.roll_down_pct)
    # 现金检查
    required_cash = new_strike * 100 * old_trade.qty_contracts
    if required_cash > state.cash + btc_cost:
        # 现金不够（注意 BTC 成本已经从 cash 扣除）
        return None

    # 新权利金报价
    q = quote_option(S, new_strike, new_dte, iv, is_call=False, r=r)
    new_premium = q.price
    new_total_premium = new_premium * 100 * old_trade.qty_contracts

    # 滚单经济性检查：新权利金至少能补回 BTC 成本的 50%
    if new_total_premium < btc_cost * 0.5:
        return None  # 滚不动，不如接货后卖 Call

    # 执行 Roll
    state.cash -= btc_cost                          # 平旧 Put 支出
    state.cash += new_total_premium                  # 卖新 Put 收入

    new_trade_obj = Trade(
        open_date=df.index[idx].date() if hasattr(df.index[idx], "date") else df.index[idx],
        close_date=df.index[new_idx].date() if hasattr(df.index[new_idx], "date") else df.index[new_idx],
        option_type="P",
        strike=new_strike,
        dte=new_dte,
        iv=iv,
        premium_per_share=new_premium,
        qty_contracts=old_trade.qty_contracts,
    )
    return {"new_trade": new_trade_obj, "btc_cost": btc_cost}


def run_wheel_backtest(df: pd.DataFrame, cfg: WheelConfig,
                       initial_capital: float = 100_000.0,
                       r: float = 0.045) -> BacktestResult:
    """
    df: 日 K 线 DataFrame，索引是 date，列包含 open, high, low, close
    cfg: WheelConfig 策略配置
    """
    df = df.sort_index()
    state = WheelState(cash=initial_capital)
    trades: list[Trade] = []
    equity_curve: list[tuple[date, float]] = []

    earnings_set = set(pd.to_datetime(d).date() for d in cfg.earnings_dates)

    # 预计算 50d MA
    df = df.copy()
    df["ma50"] = df["close"].rolling(cfg.ma_window).mean()

    for i in range(25, len(df)):   # 前 25 天用于预热 vol
        today = df.index[i].date() if hasattr(df.index[i], "date") else df.index[i]
        bar = df.iloc[i]
        S = float(bar["close"])
        S_open = float(bar["open"])
        prev_close = float(df.iloc[i-1]["close"])
        gap = (S_open - prev_close) / prev_close

        # 实现波动率（20日）→ IV 估计
        closes_20 = [float(df.iloc[j]["close"]) for j in range(i-20, i)]
        rv = _realized_vol(closes_20)
        iv = rv * cfg.iv_premium

        # ── 到期结算 ──
        if state.current_trade and state.current_trade.close_date == today:
            tr = state.current_trade
            if tr.option_type == "P":
                # ── ITM Roll：Put 到期且 ITM 时，先尝试滚单 ──
                if cfg.use_itm_roll and S < tr.strike:
                    rolled = _try_roll_put(df, i, tr, state, cfg, S, iv, r)
                    if rolled is not None:
                        # 滚单成功：标记原合约 outcome=rolled，开新 trade
                        tr.outcome = "rolled"
                        tr.pnl = (tr.premium_per_share * 100 * tr.qty_contracts
                                  - rolled["btc_cost"])
                        state.current_trade = rolled["new_trade"]
                        trades.append(rolled["new_trade"])
                        # 已 roll，跳过本轮新开仓判断
                        equity = state.cash + state.shares * S
                        equity_curve.append((today, equity))
                        continue

                # 未启用 Roll 或滚单不可行 → 正常结算
                if S < tr.strike:
                    # 被行权：现金→股票
                    cost = tr.strike * 100 * tr.qty_contracts
                    state.cash -= cost
                    state.shares += 100 * tr.qty_contracts
                    state.cost_basis = tr.strike - tr.premium_per_share  # 净成本
                    tr.outcome = "assigned"
                    tr.pnl = tr.premium_per_share * 100 * tr.qty_contracts
                else:
                    tr.outcome = "expire_otm"
                    tr.pnl = tr.premium_per_share * 100 * tr.qty_contracts
            else:  # "C"
                if S > tr.strike:
                    # 被 call 走：股票→现金
                    proceeds = tr.strike * 100 * tr.qty_contracts
                    state.cash += proceeds
                    stock_pnl = (tr.strike - state.cost_basis) * 100 * tr.qty_contracts
                    state.shares -= 100 * tr.qty_contracts
                    if state.shares <= 0:
                        state.cost_basis = 0.0
                    tr.outcome = "assigned"
                    tr.pnl = tr.premium_per_share * 100 * tr.qty_contracts + stock_pnl
                else:
                    tr.outcome = "expire_otm"
                    tr.pnl = tr.premium_per_share * 100 * tr.qty_contracts
            state.current_trade = None

        # ── 是否可开新仓 ──
        if state.current_trade is not None:
            # 持仓中，仅更新 equity
            pass
        else:
            can_open = True

            # 财报过滤：未来 dte 窗口内若有财报日，跳过
            if cfg.use_earnings_filter:
                for d_offset in range(cfg.min_dte, cfg.max_dte + 1):
                    if i + d_offset < len(df):
                        future_date = df.index[i + d_offset].date() if hasattr(df.index[i + d_offset], "date") else df.index[i + d_offset]
                        for ed in earnings_set:
                            if today <= ed <= future_date:
                                can_open = False
                                break

            # 跳空过滤：仅影响卖 Put
            if can_open and cfg.use_gap_filter and state.shares == 0:
                if gap < cfg.gap_threshold:
                    can_open = False

            # 趋势过滤：股价 < 50d MA 时不卖 CSP
            if can_open and cfg.use_ma_trend and state.shares == 0:
                ma = float(bar["ma50"]) if not pd.isna(bar["ma50"]) else S
                if S < ma:
                    can_open = False

            # IV 决定目标 delta
            if can_open:
                target_delta = _resolve_delta(cfg, iv)
                if target_delta is None:
                    can_open = False

            if can_open:
                dte, exp_idx = _next_expiry_dte(df, i, cfg)
                if exp_idx < 0:
                    can_open = False

            if can_open:
                is_call = state.shares > 0
                signed_delta = target_delta if is_call else -target_delta
                strike = find_strike_by_delta(S, signed_delta,
                                              T=dte / 365, r=r, sigma=iv,
                                              is_call=is_call)
                q = quote_option(S, strike, dte, iv, is_call=is_call, r=r)

                # 合约数
                if is_call:
                    contracts = state.shares // 100
                else:
                    contracts = _size_contracts(cfg, state.cash, strike)
                    # 确保现金足够
                    required = strike * 100 * contracts
                    if required > state.cash:
                        contracts = int(state.cash // (strike * 100))

                if contracts > 0:
                    premium = q.price
                    # 开仓：收到权利金
                    state.cash += premium * 100 * contracts
                    exp_date = df.index[exp_idx].date() if hasattr(df.index[exp_idx], "date") else df.index[exp_idx]
                    tr = Trade(
                        open_date=today,
                        close_date=exp_date,
                        option_type="C" if is_call else "P",
                        strike=strike,
                        dte=dte,
                        iv=iv,
                        premium_per_share=premium,
                        qty_contracts=contracts,
                    )
                    state.current_trade = tr
                    trades.append(tr)

        # ── 权益快照 ──
        # 期权未平仓负债近似为开仓权利金的剩余（简化为 0，到期再结算）
        equity = state.cash + state.shares * S
        equity_curve.append((today, equity))

    # ── 最终清仓 ──
    if state.shares > 0:
        final_price = float(df.iloc[-1]["close"])
        state.cash += state.shares * final_price
        state.shares = 0

    final_equity = state.cash
    total_return = (final_equity - initial_capital) / initial_capital
    days = (equity_curve[-1][0] - equity_curve[0][0]).days if len(equity_curve) > 1 else 1
    years = max(days / 365.25, 0.01)
    ann_return = (final_equity / initial_capital) ** (1 / years) - 1

    num_trades = len(trades)
    num_wins = sum(1 for t in trades if t.outcome == "expire_otm")
    num_assigned = sum(1 for t in trades if t.outcome == "assigned")
    num_rolled = sum(1 for t in trades if t.outcome == "rolled")
    win_rate = num_wins / num_trades if num_trades else 0
    total_premium = sum(t.premium_per_share * 100 * t.qty_contracts for t in trades)

    # 最大回撤
    peak = -float("inf")
    max_dd = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Sharpe
    eqs = [e for _, e in equity_curve]
    if len(eqs) > 2:
        daily_rets = [(eqs[j] - eqs[j-1]) / eqs[j-1] for j in range(1, len(eqs))]
        mean_ret = sum(daily_rets) / len(daily_rets)
        vol_d = stdev(daily_rets) if len(daily_rets) > 1 else 0
        sharpe = (mean_ret / vol_d * sqrt(252)) if vol_d > 0 else 0
    else:
        sharpe = 0

    return BacktestResult(
        config_name=cfg.name,
        initial_capital=initial_capital,
        final_equity=final_equity,
        total_return=total_return,
        annualized_return=ann_return,
        num_trades=num_trades,
        num_wins=num_wins,
        num_assigned=num_assigned,
        num_rolled=num_rolled,
        win_rate=win_rate,
        total_premium=total_premium,
        equity_curve=equity_curve,
        max_drawdown=max_dd,
        sharpe=sharpe,
        trades=trades,
    )
