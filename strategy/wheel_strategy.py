"""Wheel 期权策略核心 — 状态机 + 期权筛选"""
from __future__ import annotations

import re
from datetime import date, timedelta
from enum import Enum, auto
from typing import Optional

from alpaca.data.historical import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
from config import settings
from core.alpaca_client import AlpacaClients
from execution.option_order_manager import OptionOrderManager
from strategy.wheel_filters import (
    pre_open_put_checks, pre_open_call_checks, kelly_contracts,
    check_buying_power_sufficient,
)
from strategy.wheel_switch import maybe_switch, get_active_symbol
from strategy.wheel_evaluator import evaluate_and_maybe_plan
from loguru import logger

# 交易日志（best-effort，不影响主流程）
try:
    from metrics import trade_journal as _journal
except Exception:
    _journal = None


def _safe_journal(fn_name: str, **kwargs):
    """安全调用 trade_journal，失败仅 warn 不抛异常"""
    if _journal is None:
        return
    try:
        getattr(_journal, fn_name)(**kwargs)
    except Exception as e:
        logger.warning(f"[Journal] {fn_name} 失败: {e}")


class WheelPhase(Enum):
    IDLE       = auto()   # 无持仓，准备卖 Put
    SHORT_PUT  = auto()   # 已有 Short Put（未成交或持仓）
    LONG_STOCK = auto()   # 被行权持有股票，准备卖 Call
    SHORT_CALL = auto()   # 已有 Short Call（未成交或持仓）


def _parse_symbol(symbol: str) -> dict:
    """解析 OCC 期权代码 → {underlying, expiry, type, strike}
    格式示例：CSCO250516P00060000
    """
    m = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', symbol)
    if not m:
        return {}
    underlying, date_str, opt_type, strike_str = m.groups()
    expiry = date(2000 + int(date_str[:2]), int(date_str[2:4]), int(date_str[4:]))
    strike = int(strike_str) / 1000.0
    return {"underlying": underlying, "expiry": expiry, "type": opt_type, "strike": strike}


class WheelStrategy:
    def __init__(self, symbol: str = None):
        self.symbol = symbol or settings.WHEEL_SYMBOL
        self._trading = AlpacaClients.trading()
        self._data = OptionHistoricalDataClient(
            api_key=settings.API_KEY,
            secret_key=settings.SECRET_KEY,
        )
        self._option_mgr = OptionOrderManager()

    def _calc_total_put_collateral(self) -> float:
        """计算账户所有 Short Put 的总抵押（跨标的合计，防爆仓用）"""
        total = 0.0
        try:
            all_positions = self._trading.get_all_positions()
            for pos in all_positions:
                info = _parse_symbol(pos.symbol)
                if not info or info.get("type") != "P":
                    continue
                qty = float(pos.qty)
                if qty >= 0:  # 跳过 long put
                    continue
                total += info["strike"] * 100 * abs(qty)
        except Exception as e:
            logger.warning(f"计算总抵押失败: {e}")
        return total

    # ── 阶段检测 ──────────────────────────────────────────────────────────────

    def get_phase(self) -> tuple[WheelPhase, object]:
        """判断当前 Wheel 阶段，返回 (WheelPhase, 相关持仓/订单对象)"""

        # 1. 检查期权持仓（已成交的 short option）
        for pos in self._option_mgr.get_option_positions(self.symbol):
            info = _parse_symbol(pos.symbol)
            if not info:
                continue
            qty = float(pos.qty)
            if info["type"] == "P" and qty < 0:
                logger.info(f"持仓 Short Put: {pos.symbol} qty={qty:.0f}")
                return WheelPhase.SHORT_PUT, pos
            if info["type"] == "C" and qty < 0:
                logger.info(f"持仓 Short Call: {pos.symbol} qty={qty:.0f}")
                return WheelPhase.SHORT_CALL, pos

        # 2. 检查期权未成交订单
        for order in self._option_mgr.get_open_option_orders(self.symbol):
            info = _parse_symbol(order.symbol)
            if not info:
                continue
            if info["type"] == "P":
                logger.info(f"挂单 Short Put: {order.symbol}")
                return WheelPhase.SHORT_PUT, order
            if info["type"] == "C":
                logger.info(f"挂单 Short Call: {order.symbol}")
                return WheelPhase.SHORT_CALL, order

        # 3. 检查股票持仓（被行权后持有 ≥100 股）
        try:
            pos = self._trading.get_open_position(self.symbol)
            qty = float(pos.qty)
            if qty >= 100:
                logger.info(f"持仓股票: {self.symbol} x{qty:.0f} 股 @ 成本 {pos.avg_entry_price}")
                return WheelPhase.LONG_STOCK, pos
        except Exception:
            pass  # 无股票持仓

        return WheelPhase.IDLE, None

    # ── 期权链 ─────────────────────────────────────────────────────────────────

    def _fetch_chain(self, contract_type: str) -> dict:
        """获取 WHEEL_MIN_DTE~WHEEL_MAX_DTE 的期权链"""
        today = date.today()
        dte_min = today + timedelta(days=settings.WHEEL_MIN_DTE)
        dte_max = today + timedelta(days=settings.WHEEL_MAX_DTE)
        try:
            req = OptionChainRequest(
                underlying_symbol=self.symbol,
                expiration_date_gte=dte_min,
                expiration_date_lte=dte_max,
                type=contract_type,
            )
            chain = self._data.get_option_chain(req)
            logger.info(f"期权链: {len(chain)} 个 {contract_type.upper()} 合约")
            return chain
        except Exception as e:
            logger.error(f"获取期权链失败: {e}")
            return {}

    def select_put(self) -> Optional[tuple[str, float]]:
        """筛选 delta ≈ -WHEEL_TARGET_DELTA 的 Put，返回 (symbol, mid_price)"""
        chain = self._fetch_chain("put")
        target = -settings.WHEEL_TARGET_DELTA
        best_sym, best_snap, best_diff = None, None, float("inf")

        for sym, snap in chain.items():
            if not snap.greeks or snap.greeks.delta is None:
                continue
            if not snap.latest_quote:
                continue
            diff = abs(snap.greeks.delta - target)
            if diff < best_diff:
                best_diff = diff
                best_sym = sym
                best_snap = snap

        if best_sym is None:
            logger.warning("未找到合适的 Put 合约")
            return None

        bid = float(best_snap.latest_quote.bid_price or 0)
        ask = float(best_snap.latest_quote.ask_price or 0)
        mid = round((bid + ask) / 2, 2)
        info = _parse_symbol(best_sym)
        logger.info(
            f"选定 Put: {best_sym} | 执行价={info['strike']:.2f} "
            f"到期={info['expiry']} Δ={best_snap.greeks.delta:.3f} mid={mid:.2f}"
        )
        return best_sym, mid

    def select_call(self, cost_basis: float) -> Optional[tuple[str, float]]:
        """筛选 delta ≈ +WHEEL_TARGET_DELTA 且执行价 ≥ cost_basis 的 Call"""
        chain = self._fetch_chain("call")
        target = settings.WHEEL_TARGET_DELTA
        best_sym, best_snap, best_diff = None, None, float("inf")

        for sym, snap in chain.items():
            if not snap.greeks or snap.greeks.delta is None:
                continue
            if not snap.latest_quote:
                continue
            info = _parse_symbol(sym)
            if not info or info["strike"] < cost_basis:
                continue  # 执行价必须 ≥ 成本价，避免锁定亏损
            diff = abs(snap.greeks.delta - target)
            if diff < best_diff:
                best_diff = diff
                best_sym = sym
                best_snap = snap

        if best_sym is None:
            logger.warning(f"未找到执行价 ≥ {cost_basis:.2f} 的 Call 合约")
            return None

        bid = float(best_snap.latest_quote.bid_price or 0)
        ask = float(best_snap.latest_quote.ask_price or 0)
        mid = round((bid + ask) / 2, 2)
        info = _parse_symbol(best_sym)
        logger.info(
            f"选定 Call: {best_sym} | 执行价={info['strike']:.2f} "
            f"到期={info['expiry']} Δ={best_snap.greeks.delta:.3f} mid={mid:.2f}"
        )
        return best_sym, mid

    # ── 主循环 ─────────────────────────────────────────────────────────────────

    def run_cycle(self):
        """检查当前阶段并执行相应动作（幂等）"""
        phase, obj = self.get_phase()
        logger.info(f"── Wheel 阶段: {phase.name} ──")

        is_idle = (phase == WheelPhase.IDLE)

        # ── 每轮评估：检查当前标的是否该切换（STOP/CAUTION/更优候选）──
        try:
            decision = evaluate_and_maybe_plan(
                current_symbol=self.symbol,
                current_phase_is_idle=is_idle,
            )
            logger.info(f"📊 评估: {decision['message']}")
            self.last_decision = decision
        except Exception as e:
            logger.warning(f"每轮评估失败（继续执行）: {e}")
            self.last_decision = None

        # ── 切换检查：若计划切换且当前 IDLE，则触发切换并直接返回，等下一 cron ──
        switched = maybe_switch(current_phase_is_idle=is_idle)
        if switched:
            old_sym, new_sym = switched
            logger.warning(f"🔄 已从 {old_sym} 切换到 {new_sym}，本次周期跳过，下一轮用新标的")
            # 更新实例标的以便本轮摘要正确
            self.symbol = new_sym
            return

        if phase == WheelPhase.IDLE:
            # ── 回测验证的过滤器：财报/MA50趋势/极端波动 ──
            ok, reason = pre_open_put_checks(self.symbol)
            if not ok:
                logger.warning(f"⏸️  跳过卖 Put: {reason}")
                # 记录跳过原因，识别哪个过滤器触发
                fname = "earnings" if "财报" in reason or "earnings" in reason.lower() else \
                        "ma_trend" if "MA" in reason or "trend" in reason.lower() else \
                        "vol" if "波动" in reason or "vol" in reason.lower() else "other"
                _safe_journal("log_skip", symbol=self.symbol, action="sell_put",
                              skip_reason=reason, filter_name=fname)
                return
            if reason:
                logger.info(f"过滤检查通过 | {reason}")

            result = self.select_put()
            if result:
                sym, mid = result
                if mid <= 0:
                    logger.warning(f"权利金 mid={mid:.2f}，价格异常，跳过")
                    _safe_journal("log_skip", symbol=self.symbol, action="sell_put",
                                  skip_reason=f"premium_invalid={mid:.2f}")
                    return

                # 动态仓位（资金驱动，4 层防护）
                info = _parse_symbol(sym)
                strike = info["strike"] if info else 0
                acct = self._trading.get_account()
                cash = float(acct.cash)
                bp = float(acct.buying_power)
                equity = float(acct.equity)
                # 计算账户所有 Short Put 的总抵押（考虑跨标的组合风险）
                existing_collateral = self._calc_total_put_collateral()
                contracts = kelly_contracts(
                    cash=cash, strike=strike, premium=mid,
                    buying_power=bp, equity=equity,
                    existing_put_collateral=existing_collateral,
                )
                if contracts < 1:
                    logger.warning(f"⏸️ 资金不足，跳过开仓 (cash=${cash:,.0f} bp=${bp:,.0f} existing=${existing_collateral:,.0f})")
                    _safe_journal("log_skip", symbol=self.symbol, action="sell_put",
                                  skip_reason=f"insufficient_funds")
                    return
                # ★ 下单前硬检查（最后一道防线）
                bp_ok, bp_msg = check_buying_power_sufficient(
                    strike=strike, qty=contracts,
                    buying_power=bp, equity=equity,
                )
                if not bp_ok:
                    logger.error(f"🛑 BP 硬检查失败: {bp_msg} — 取消开仓")
                    _safe_journal("log_skip", symbol=self.symbol, action="sell_put",
                                  skip_reason=bp_msg)
                    return
                logger.info(f"✓ 资金检查通过: 开 {contracts} 张 | {bp_msg}")
                self._option_mgr.sell_to_open(sym, contracts, mid)
                # 记录开仓
                expiry_str = info["expiry"].isoformat() if info and info.get("expiry") else ""
                dte = (info["expiry"] - date.today()).days if info and info.get("expiry") else None
                _safe_journal("log_entry",
                              symbol=self.symbol, action="sell_put",
                              contract=sym, strike=strike, expiry=expiry_str,
                              qty=contracts, premium=mid, dte=dte,
                              filters_passed={"earnings": True, "ma_trend": True, "vol": True},
                              notes=f"kelly={contracts}")

        elif phase == WheelPhase.LONG_STOCK:
            # Covered Call 只过滤财报（必须继续卖以回收溢价）
            ok, reason = pre_open_call_checks(self.symbol)
            if not ok:
                logger.warning(f"⏸️  跳过卖 Call: {reason}")
                _safe_journal("log_skip", symbol=self.symbol, action="sell_call",
                              skip_reason=reason, filter_name="earnings")
                return

            cost_basis = float(obj.avg_entry_price)
            result = self.select_call(cost_basis)
            if result:
                sym, mid = result
                if mid <= 0:
                    logger.warning(f"权利金 mid={mid:.2f}，价格异常，跳过")
                    _safe_journal("log_skip", symbol=self.symbol, action="sell_call",
                                  skip_reason=f"premium_invalid={mid:.2f}")
                    return
                # CC 合约数严格由持股数决定（每 100 股支持 1 张 covered call）
                # 卖 CC 不占现金（股票即抵押），不存在爆仓/平仓风险
                contracts = int(float(obj.qty) // 100)
                if contracts < 1:
                    logger.warning(f"持股 {obj.qty} < 100，不够 1 张 CC")
                    return
                self._option_mgr.sell_to_open(sym, contracts, mid)
                # 记录开仓
                info = _parse_symbol(sym)
                strike = info["strike"] if info else 0
                expiry_str = info["expiry"].isoformat() if info and info.get("expiry") else ""
                dte = (info["expiry"] - date.today()).days if info and info.get("expiry") else None
                _safe_journal("log_entry",
                              symbol=self.symbol, action="sell_call",
                              contract=sym, strike=strike, expiry=expiry_str,
                              qty=contracts, premium=mid, dte=dte,
                              filters_passed={"earnings": True},
                              notes=f"cost_basis={cost_basis:.2f}")

        elif phase in (WheelPhase.SHORT_PUT, WheelPhase.SHORT_CALL):
            logger.info("期权已开仓，持仓中，等待到期或行权...")
