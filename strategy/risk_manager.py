"""风险控制模块"""
from __future__ import annotations
from core.exceptions import CircuitBreakerError, RiskLimitExceededError
from config import settings
from loguru import logger


class RiskManager:
    def __init__(self, initial_equity: float):
        self.initial_equity = initial_equity
        self.peak_equity    = initial_equity
        self.daily_start_equity = initial_equity
        self._circuit_broken = False

    def update_equity(self, equity: float):
        self.peak_equity = max(self.peak_equity, equity)

    def reset_daily(self, equity: float):
        self.daily_start_equity = equity
        self._circuit_broken = False
        logger.info(f"日内风控重置，当日起始权益: {equity:.2f}")

    def check_daily_loss(self, equity: float):
        """检查日内亏损熔断"""
        if self._circuit_broken:
            raise CircuitBreakerError("熔断已触发，当日停止新开仓")
        daily_loss = (self.daily_start_equity - equity) / self.daily_start_equity
        if daily_loss >= settings.MAX_DAILY_LOSS:
            self._circuit_broken = True
            logger.warning(f"熔断触发！日内亏损 {daily_loss:.1%} 超过 {settings.MAX_DAILY_LOSS:.1%}")
            raise CircuitBreakerError(f"日内亏损 {daily_loss:.1%}")

    def check_position_count(self, open_positions: int):
        """检查持仓数量上限"""
        if open_positions >= settings.MAX_OPEN_POSITIONS:
            raise RiskLimitExceededError(
                f"持仓数量 {open_positions} 已达上限 {settings.MAX_OPEN_POSITIONS}"
            )

    def check_trade_loss(self, entry_price: float, current_price: float,
                         side: str, equity: float):
        """检查单笔亏损上限"""
        pnl_ratio = (current_price - entry_price) / entry_price
        if side == "sell":
            pnl_ratio = -pnl_ratio
        if pnl_ratio < -settings.MAX_TRADE_LOSS:
            raise RiskLimitExceededError(
                f"单笔亏损 {pnl_ratio:.1%} 超过上限 {settings.MAX_TRADE_LOSS:.1%}"
            )

    @property
    def is_circuit_broken(self) -> bool:
        return self._circuit_broken
