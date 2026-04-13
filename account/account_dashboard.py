"""账户仪表盘 — 余额、持仓、历史订单"""
from __future__ import annotations
from core.alpaca_client import AlpacaClients
from execution.order_manager import OrderManager
from execution.portfolio_manager import PortfolioManager
from loguru import logger


class AccountDashboard:
    def __init__(self):
        self._client    = AlpacaClients.trading()
        self._orders    = OrderManager()
        self._portfolio = PortfolioManager()

    def show_account(self):
        acct = self._client.get_account()
        print("\n" + "=" * 60)
        print("  ALPACA PAPER TRADING 账户概览")
        print("=" * 60)
        print(f"  账户 ID    : {acct.id}")
        print(f"  账户状态   : {acct.status}")
        print(f"  总权益     : ${float(acct.equity):>12,.2f}")
        print(f"  投资组合   : ${float(acct.portfolio_value):>12,.2f}")
        print(f"  现金       : ${float(acct.cash):>12,.2f}")
        print(f"  购买力     : ${float(acct.buying_power):>12,.2f}")
        print(f"  日内交易   : {'受限 (PDT)' if acct.pattern_day_trader else '正常'}")
        print(f"  PDT 余次   : {acct.daytrade_count}/3 (滚动5个交易日)")
        print("=" * 60)

    def show_positions(self):
        positions = self._portfolio.get_positions()
        print("\n" + "=" * 60)
        print("  当前持仓")
        print("=" * 60)
        if not positions:
            print("  无持仓")
        for sym, p in positions.items():
            color = "▲" if p["unrealized_pl"] >= 0 else "▼"
            print(
                f"  {color} {sym:6s}  数量:{p['qty']:>6.0f}  "
                f"成本:{p['avg_entry']:>8.2f}  "
                f"市值:{p['market_value']:>10,.2f}  "
                f"盈亏:{p['unrealized_pl']:>+9.2f} ({float(p['unrealized_plpc'])*100:+.2f}%)"
            )
        print("=" * 60)

    def show_open_orders(self):
        orders = self._orders.get_open_orders()
        print("\n" + "=" * 60)
        print("  未成交订单")
        print("=" * 60)
        if not orders:
            print("  无未成交订单")
        for o in orders:
            print(
                f"  {o.symbol:6s}  {str(o.side):4s}  "
                f"数量:{o.qty}  类型:{o.type}  "
                f"状态:{o.status}  id:{str(o.id)[:8]}..."
            )
        print("=" * 60)

    def show_recent_orders(self, limit: int = 20):
        orders = self._orders.get_all_orders(limit=limit)
        print("\n" + "=" * 60)
        print(f"  最近 {limit} 笔订单")
        print("=" * 60)
        for o in orders:
            filled_price = f"@{float(o.filled_avg_price):.2f}" if o.filled_avg_price else ""
            print(
                f"  {str(o.submitted_at)[:19]}  {o.symbol:6s}  "
                f"{str(o.side):4s}  数量:{o.qty}  "
                f"{filled_price}  状态:{o.status}"
            )
        print("=" * 60)

    def show_all(self):
        self.show_account()
        self.show_positions()
        self.show_open_orders()
        self.show_recent_orders()


if __name__ == "__main__":
    import sys
    sys.path.insert(0, __import__("os").path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import os
    dash = AccountDashboard()
    dash.show_all()
