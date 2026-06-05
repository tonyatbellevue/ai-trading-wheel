"""期权订单管理 — Sell to Open / Buy to Close"""
from __future__ import annotations

import hashlib
from datetime import date

from alpaca.trading.requests import LimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce, QueryOrderStatus
from core.alpaca_client import AlpacaClients
from core.exceptions import OrderError
from loguru import logger


def _make_client_order_id(action: str, option_symbol: str, qty: int) -> str:
    """Phase 2 (P2.1): deterministic client_order_id for idempotency.

    Same (action, symbol, qty, today) → same id → Alpaca rejects the
    duplicate server-side. Prevents the #31 risk: if get_open_option_orders
    fails mid-cycle, the idempotency check could fall through and place a
    SECOND BTC/STO on the same contract. With a deterministic id, the
    second submit is a no-op (Alpaca 422 duplicate, caught as success).

    Includes today's date so a legitimately re-opened position on a later
    day still gets a fresh id.
    """
    raw = f"{action}|{option_symbol}|{qty}|{date.today().isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class OptionOrderManager:
    def __init__(self):
        self._client = AlpacaClients.trading()

    # ── 下单 ─────────────────────────────────────────────────────────────────

    def _submit_idempotent(self, action: str, option_symbol: str, qty: int,
                            side: OrderSide, limit_price: float) -> str:
        """Submit a LIMIT order with a deterministic client_order_id.
        Treats Alpaca's duplicate-id rejection as success (idempotent)."""
        coid = _make_client_order_id(action, option_symbol, qty)
        try:
            req = LimitOrderRequest(
                symbol=option_symbol,
                qty=qty,
                side=side,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
                client_order_id=coid,
            )
            order = self._client.submit_order(req)
            logger.info(f"[{action}] {option_symbol} x{qty} @ {limit_price:.2f} | order_id={order.id} coid={coid}")
            return str(order.id)
        except Exception as e:
            msg = str(e).lower()
            # Alpaca rejects duplicate client_order_id → idempotent no-op.
            # IMPORTANT (risk review fix): match ONLY duplicate-id semantics.
            # Do NOT match bare "422" — Alpaca returns 422 for many rejects
            # (insufficient buying power, invalid price, market closed). A
            # broad "422" match would silently swallow a real order failure
            # and make the bot think it traded when it didn't.
            is_dup = ("client_order_id" in msg and "exist" in msg) \
                or "duplicate" in msg \
                or "already exists" in msg
            if is_dup:
                logger.warning(
                    f"[{action}] {option_symbol} 幂等拦截: 重复 coid={coid}, "
                    f"视为已下单 (no-op)"
                )
                return f"idempotent-noop:{coid}"
            logger.error(f"[{action}] {option_symbol} 失败: {e}")
            raise OrderError(str(e)) from e

    def sell_to_open(self, option_symbol: str, qty: int, limit_price: float) -> str:
        """卖出开仓（Sell to Open）"""
        return self._submit_idempotent("STO", option_symbol, qty, OrderSide.SELL, limit_price)

    def buy_to_close(self, option_symbol: str, qty: int, limit_price: float) -> str:
        """买入平仓（Buy to Close）"""
        return self._submit_idempotent("BTC", option_symbol, qty, OrderSide.BUY, limit_price)

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
