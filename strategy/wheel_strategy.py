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
from loguru import logger


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

        if phase == WheelPhase.IDLE:
            result = self.select_put()
            if result:
                sym, mid = result
                if mid <= 0:
                    logger.warning(f"权利金 mid={mid:.2f}，价格异常，跳过")
                    return
                self._option_mgr.sell_to_open(sym, settings.WHEEL_CONTRACTS, mid)

        elif phase == WheelPhase.LONG_STOCK:
            cost_basis = float(obj.avg_entry_price)
            result = self.select_call(cost_basis)
            if result:
                sym, mid = result
                if mid <= 0:
                    logger.warning(f"权利金 mid={mid:.2f}，价格异常，跳过")
                    return
                self._option_mgr.sell_to_open(sym, settings.WHEEL_CONTRACTS, mid)

        elif phase in (WheelPhase.SHORT_PUT, WheelPhase.SHORT_CALL):
            logger.info("期权已开仓，持仓中，等待到期或行权...")
