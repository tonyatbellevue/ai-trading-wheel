"""期权订单管理 — Sell to Open / Buy to Close"""
from __future__ import annotations

from alpaca.trading.requests import LimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce, QueryOrderStatus
from core.alpaca_client import AlpacaClients
from core.exceptions import OrderError
from loguru import logger


class OptionOrderManager:
    def __init__(self):
        self._client = AlpacaClients.trading()

    # ── 下单 ─────────────────────────────────────────────────────────────────

    def sell_to_open(self, option_symbol: str, qty: int, limit_price: float) -> str:
        """卖出开仓（Sell to Open）"""
        try:
            req = LimitOrderRequest(
                symbol=option_symbol,
                qty=qty,
                side=OrderSide.SELL,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
            )
            order = self._client.submit_order(req)
            logger.info(f"[STO] {option_symbol} x{qty} @ {limit_price:.2f} | order_id={order.id}")
            return str(order.id)
        except Exception as e:
            logger.error(f"[STO] {option_symbol} 失败: {e}")
            raise OrderError(str(e)) from e

    def buy_to_close(self, option_symbol: str, qty: int, limit_price: float) -> str:
        """买入平仓（Buy to Close）"""
        try:
            req = LimitOrderRequest(
                symbol=option_symbol,
                qty=qty,
                side=OrderSide.BUY,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
            )
            order = self._client.submit_order(req)
            logger.info(f"[BTC] {option_symbol} x{qty} @ {limit_price:.2f} | order_id={order.id}")
            return str(order.id)
        except Exception as e:
            logger.error(f"[BTC] {option_symbol} 失败: {e}")
            raise OrderError(str(e)) from e

    # ── 查询 ─────────────────────────────────────────────────────────────────

    def get_open_option_orders(self, underlying: str) -> list:
        """获取指定标的的所有未成交期权订单"""
        try:
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            orders = self._client.get_orders(req)
            # 期权代码比股票代码长（含日期+执行价）
            return [o for o in orders if o.symbol.startswith(underlying) and len(o.symbol) > 6]
        except Exception as e:
            logger.error(f"获取未成交期权订单失败: {e}")
            return []

    def get_option_positions(self, underlying: str) -> list:
        """获取指定标的的所有期权持仓"""
        try:
            positions = self._client.get_all_positions()
            return [p for p in positions if p.symbol.startswith(underlying) and len(p.symbol) > 6]
        except Exception as e:
            logger.error(f"获取期权持仓失败: {e}")
            return []

    def cancel_order(self, order_id: str):
        try:
            self._client.cancel_order_by_id(order_id)
            logger.info(f"期权订单 {order_id} 已撤销")
        except Exception as e:
            logger.error(f"撤销失败 {order_id}: {e}")
