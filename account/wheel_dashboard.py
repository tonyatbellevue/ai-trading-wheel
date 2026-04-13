"""Wheel 期权策略仪表盘 — 状态、持仓、历史、收益汇总"""
from __future__ import annotations

from datetime import date
from typing import Optional

from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest, StockLatestQuoteRequest
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

from config import settings
from core.alpaca_client import AlpacaClients
from execution.option_order_manager import OptionOrderManager
from strategy.wheel_strategy import WheelStrategy, WheelPhase, _parse_symbol

SEP = "=" * 66

_PHASE_LABEL = {
    WheelPhase.IDLE:       "[ ] IDLE        -- 无持仓，等待开仓",
    WheelPhase.SHORT_PUT:  "[P] SHORT PUT   -- 现金担保 Put 已卖出",
    WheelPhase.LONG_STOCK: "[S] LONG STOCK  -- 被行权，持有股票",
    WheelPhase.SHORT_CALL: "[C] SHORT CALL  -- Covered Call 已卖出",
}


class WheelDashboard:
    def __init__(self, symbol: str = None):
        self.symbol    = symbol or settings.WHEEL_SYMBOL
        self._trading  = AlpacaClients.trading()
        self._opt_data = OptionHistoricalDataClient(
            api_key=settings.API_KEY, secret_key=settings.SECRET_KEY
        )
        self._stk_data = StockHistoricalDataClient(
            api_key=settings.API_KEY, secret_key=settings.SECRET_KEY
        )
        self._option_mgr = OptionOrderManager()
        self._strategy   = WheelStrategy(self.symbol)

    # ── 数据获取 ──────────────────────────────────────────────────────────────

    def _stock_price(self) -> Optional[float]:
        try:
            resp = self._stk_data.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=self.symbol)
            )
            q = resp[self.symbol]
            return round((float(q.bid_price) + float(q.ask_price)) / 2, 2)
        except Exception:
            return None

    def _option_snapshot(self, symbols: list[str]) -> dict:
        if not symbols:
            return {}
        try:
            return self._opt_data.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=symbols)
            )
        except Exception:
            return {}

    def _all_option_orders(self, limit: int = 100) -> list:
        try:
            req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit)
            orders = self._trading.get_orders(req)
            return [
                o for o in orders
                if o.symbol.startswith(self.symbol) and len(o.symbol) > 6
            ]
        except Exception:
            return []

    # ── 展示区块 ──────────────────────────────────────────────────────────────

    def _show_header(self, stock_price: Optional[float]):
        phase, _ = self._strategy.get_phase()
        price_str = f"${stock_price:.2f}" if stock_price else "N/A"
        print("\n" + SEP)
        print("  CSCO OPTION WHEEL 策略仪表盘")
        print(SEP)
        print(f"  标的 : {self.symbol}    当前价 : {price_str}    日期 : {date.today()}")
        print(f"  阶段 : {_PHASE_LABEL[phase]}")
        print(SEP)

    def _show_stock_position(self):
        try:
            pos = self._trading.get_open_position(self.symbol)
            qty = float(pos.qty)
            if qty < 1:
                return
            cost  = float(pos.avg_entry_price)
            mkt   = float(pos.market_value)
            pl    = float(pos.unrealized_pl)
            plpct = float(pos.unrealized_plpc) * 100
            sign  = "+" if pl >= 0 else "-"
            print(f"\n  [股票持仓]")
            print(f"  {self.symbol}  数量:{qty:.0f} 股  成本:${cost:.2f}  市值:${mkt:,.2f}  "
                  f"盈亏:{pl:+.2f} ({plpct:+.2f}%)")
            print("  " + "-" * 62)
        except Exception:
            pass

    def _show_option_positions(self, stock_price: Optional[float]):
        positions = self._option_mgr.get_option_positions(self.symbol)
        print(f"\n  [期权持仓]")
        if not positions:
            print("  无期权持仓")
        else:
            snaps = self._option_snapshot([p.symbol for p in positions])
            today = date.today()
            print(f"  {'合约':<24} {'类型':<5} {'执行价':>7} {'到期日':<12} {'DTE':>4} "
                  f"{'qty':>5} {'成本':>6} {'现价':>6} {'盈亏':>9} {'Delta':>7}")
            print("  " + "-" * 86)
            for pos in positions:
                info  = _parse_symbol(pos.symbol)
                if not info:
                    continue
                snap  = snaps.get(pos.symbol)
                qty   = float(pos.qty)
                cost  = float(pos.avg_entry_price or 0)
                dte   = (info["expiry"] - today).days
                otype = "PUT" if info["type"] == "P" else "CALL"

                curr = 0.0
                if snap and snap.latest_quote:
                    bid = float(snap.latest_quote.bid_price or 0)
                    ask = float(snap.latest_quote.ask_price or 0)
                    curr = round((bid + ask) / 2, 2)

                pl = round((cost - curr) * abs(qty) * 100, 2) if qty < 0 \
                     else round((curr - cost) * abs(qty) * 100, 2)

                delta_str = f"{snap.greeks.delta:.3f}" if (snap and snap.greeks and snap.greeks.delta is not None) else "  N/A"

                print(f"  {pos.symbol:<24} {otype:<5} {info['strike']:>7.2f} "
                      f"{str(info['expiry']):<12} {dte:>4} {qty:>5.0f} "
                      f"{cost:>6.2f} {curr:>6.2f} {pl:>+9.2f} {delta_str:>7}")

                # ITM/OTM 状态
                if stock_price:
                    strike = info["strike"]
                    if info["type"] == "P":
                        itm = stock_price < strike
                        pad = abs(stock_price - strike)
                    else:
                        itm = stock_price > strike
                        pad = abs(strike - stock_price)
                    status = f"!! ITM (安全垫 -${pad:.2f})" if itm else f"   OTM (安全垫 +${pad:.2f})"
                    print(f"  {' ' * 24} {status}")

        print("  " + "-" * 62)

    def _show_open_orders(self):
        orders = self._option_mgr.get_open_option_orders(self.symbol)
        print(f"\n  [未成交期权订单]")
        if not orders:
            print("  无未成交订单")
        else:
            today = date.today()
            for o in orders:
                info      = _parse_symbol(o.symbol)
                dte       = (info["expiry"] - today).days if info else 0
                side_str  = str(o.side).split(".")[-1]
                stat_str  = str(o.status).split(".")[-1]
                lmt_str   = f"Limit ${float(o.limit_price):.2f}" if o.limit_price else ""
                print(f"  {o.symbol:<26} {side_str:<5} x{o.qty}  {lmt_str:<14}  DTE:{dte:>3}  {stat_str}")
        print("  " + "-" * 62)

    def _show_history(self, limit: int = 20):
        orders = self._all_option_orders(limit)
        print(f"\n  [期权交易历史 -- 最近 {limit} 笔]")
        if not orders:
            print("  暂无历史记录")
        else:
            print(f"  {'时间':<20} {'合约':<26} {'方向':<5} {'成交价':>8}  {'状态'}")
            print("  " + "-" * 72)
            for o in orders[:limit]:
                side_str  = str(o.side).split(".")[-1]
                stat_str  = str(o.status).split(".")[-1]
                price_str = f"${float(o.filled_avg_price):.2f}" if o.filled_avg_price else " 挂单中"
                ts = str(o.submitted_at)[:19] if o.submitted_at else "—"
                print(f"  {ts:<20} {o.symbol:<26} {side_str:<5} {price_str:>8}  {stat_str}")
        print("  " + "-" * 62)

    def _show_summary(self):
        orders = self._all_option_orders(limit=200)
        filled = [o for o in orders if o.filled_avg_price]

        total_premium = 0.0
        total_buyback = 0.0
        rounds        = 0

        for o in filled:
            side  = str(o.side).split(".")[-1].upper()
            price = float(o.filled_avg_price)
            qty   = float(o.qty or 1)
            value = price * qty * 100
            if side == "SELL":
                total_premium += value
                rounds += 1
            else:
                total_buyback += value

        realized_pl = total_premium - total_buyback
        print(f"\n  [收益汇总]")
        print(f"  累计收取权利金 : ${total_premium:>10,.2f}   (STO 总收入)")
        print(f"  买回平仓支出   : ${total_buyback:>10,.2f}   (BTC 总支出)")
        print(f"  期权已实现盈亏 : ${realized_pl:>10,.2f}")
        print(f"  开仓次数       : {rounds:>4} 次")
        print("\n" + SEP)

    def show(self):
        stock_price = self._stock_price()
        self._show_header(stock_price)
        self._show_stock_position()
        self._show_option_positions(stock_price)
        self._show_open_orders()
        self._show_history()
        self._show_summary()
