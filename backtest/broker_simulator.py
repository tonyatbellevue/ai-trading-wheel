"""模拟撮合引擎 — 滑点、手续费、止损"""
from __future__ import annotations
import random
from dataclasses import dataclass, field
from config import settings
from loguru import logger


@dataclass
class SimPosition:
    symbol:      str
    qty:         float
    entry_price: float
    side:        str         # "long" / "short"
    stop_price:  float = 0.0


@dataclass
class SimBroker:
    initial_capital: float = settings.INITIAL_CAPITAL
    commission:      float = settings.COMMISSION_PER_SHARE
    slippage:        float = settings.SLIPPAGE_FACTOR

    cash:      float = field(init=False)
    positions: dict  = field(default_factory=dict)
    trades:    list  = field(default_factory=list)
    equity_curve: list = field(default_factory=list)

    def __post_init__(self):
        self.cash = self.initial_capital

    def _fill_price(self, price: float, side: str, atr: float = 0.0) -> float:
        """模拟滑点: 随机在 ATR 的 0-5% 范围内偏移"""
        slip = abs(random.uniform(0, self.slippage)) * price
        if side == "buy":
            return price + slip
        return price - slip

    def buy(self, symbol: str, qty: int, bar, atr: float = 0.0) -> bool:
        """
        以下一根 K 线开盘价成交（bar 为下一根 K 线数据）
        """
        fill_price = self._fill_price(float(bar["open"]), "buy", atr)
        cost       = fill_price * qty + self.commission * qty

        if cost > self.cash:
            logger.debug(f"[{symbol}] 资金不足: 需要 {cost:.2f}，现金 {self.cash:.2f}")
            return False

        self.cash -= cost
        self.positions[symbol] = SimPosition(
            symbol=symbol, qty=qty, entry_price=fill_price,
            side="long",
            stop_price=fill_price - atr * settings.ATR_STOP_MULTIPLIER,
        )
        self.trades.append({
            "symbol": symbol, "side": "buy", "qty": qty,
            "price": fill_price, "ts": bar.name,
        })
        logger.debug(f"[{symbol}] 模拟买入 {qty} 股 @ {fill_price:.2f}")
        return True

    def sell(self, symbol: str, bar, reason: str = "signal") -> bool:
        if symbol not in self.positions:
            return False
        pos        = self.positions[symbol]
        fill_price = self._fill_price(float(bar["open"]), "sell", 0)
        proceeds   = fill_price * pos.qty - self.commission * pos.qty
        pnl        = (fill_price - pos.entry_price) * pos.qty

        self.cash += proceeds
        self.trades.append({
            "symbol": symbol, "side": "sell", "qty": pos.qty,
            "price": fill_price, "ts": bar.name,
            "pnl": pnl, "reason": reason,
        })
        del self.positions[symbol]
        logger.debug(f"[{symbol}] 模拟卖出 @ {fill_price:.2f} | PnL={pnl:+.2f} ({reason})")
        return True

    def check_stops(self, bar) -> list[str]:
        """检查止损线，返回触发止损的标的列表"""
        triggered = []
        for sym, pos in list(self.positions.items()):
            if pos.side == "long" and float(bar["low"]) <= pos.stop_price:
                triggered.append(sym)
        return triggered

    def mark_to_market(self, prices: dict[str, float]) -> float:
        """按当前价格计算总权益"""
        position_value = sum(
            pos.qty * prices.get(pos.symbol, pos.entry_price)
            for pos in self.positions.values()
        )
        equity = self.cash + position_value
        self.equity_curve.append(equity)
        return equity

    @property
    def total_trades(self) -> int:
        return len([t for t in self.trades if t["side"] == "sell"])

    @property
    def winning_trades(self) -> int:
        return len([t for t in self.trades if t.get("pnl", 0) > 0])
