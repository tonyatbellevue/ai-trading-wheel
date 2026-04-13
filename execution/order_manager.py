"""订单管理 — 下单、查询、撤单"""
from __future__ import annotations
from alpaca.trading.requests import (
    MarketOrderRequest, StopOrderRequest, GetOrdersRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus, QueryOrderStatus
from core.alpaca_client import AlpacaClients
from core.exceptions import OrderError
from config import settings
from loguru import logger


class OrderManager:
    def __init__(self):
        self._client = AlpacaClients.trading()

    # ── 下单 ─────────────────────────────────────────────────────────
    def market_buy(self, symbol: str, qty: int) -> str:
        """市价买入，返回 order_id"""
        try:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
            order = self._client.submit_order(req)
            logger.info(f"[{symbol}] 市价买入 {qty} 股 | order_id={order.id}")
            return str(order.id)
        except Exception as e:
            logger.error(f"[{symbol}] 买入失败: {e}")
            raise OrderError(str(e)) from e

    def market_sell(self, symbol: str, qty: int) -> str:
        """市价卖出，返回 order_id"""
        try:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            order = self._client.submit_order(req)
            logger.info(f"[{symbol}] 市价卖出 {qty} 股 | order_id={order.id}")
            return str(order.id)
        except Exception as e:
            logger.error(f"[{symbol}] 卖出失败: {e}")
            raise OrderError(str(e)) from e

    def stop_loss(self, symbol: str, qty: int, stop_price: float) -> str:
        """止损单"""
        try:
            req = StopOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=round(stop_price, 2),
            )
            order = self._client.submit_order(req)
            logger.info(f"[{symbol}] 止损单 stop={stop_price:.2f} | order_id={order.id}")
            return str(order.id)
        except Exception as e:
            logger.error(f"[{symbol}] 止损单失败: {e}")
            raise OrderError(str(e)) from e

    # ── 查询 ─────────────────────────────────────────────────────────
    def get_open_orders(self) -> list:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        return self._client.get_orders(req)

    def get_all_orders(self, limit: int = 50) -> list:
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit)
        return self._client.get_orders(req)

    def cancel_order(self, order_id: str):
        try:
            self._client.cancel_order_by_id(order_id)
            logger.info(f"订单 {order_id} 已撤销")
        except Exception as e:
            logger.error(f"撤单失败 {order_id}: {e}")

    def cancel_all_orders(self):
        self._client.cancel_orders()
        logger.info("所有未成交订单已撤销")

    # ── 平仓 ─────────────────────────────────────────────────────────
    def close_position(self, symbol: str):
        try:
            self._client.close_position(symbol)
            logger.info(f"[{symbol}] 持仓已平")
        except Exception as e:
            logger.error(f"[{symbol}] 平仓失败: {e}")

    def close_all_positions(self):
        self._client.close_all_positions(cancel_orders=True)
        logger.info("所有持仓已平")
