"""持仓与账户查询"""
from __future__ import annotations
from core.alpaca_client import AlpacaClients
from loguru import logger


class PortfolioManager:
    def __init__(self):
        self._client = AlpacaClients.trading()

    def get_equity(self) -> float:
        acct = self._client.get_account()
        return float(acct.equity)

    def get_buying_power(self) -> float:
        acct = self._client.get_account()
        return float(acct.buying_power)

    def get_positions(self) -> dict[str, dict]:
        """返回 {symbol: {qty, market_value, unrealized_pl, unrealized_plpc}}"""
        positions = self._client.get_all_positions()
        result = {}
        for p in positions:
            result[p.symbol] = {
                "qty":            float(p.qty),
                "avg_entry":      float(p.avg_entry_price),
                "market_value":   float(p.market_value),
                "unrealized_pl":  float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
                "side":           str(p.side),
            }
        return result

    def get_position(self, symbol: str) -> dict | None:
        positions = self.get_positions()
        return positions.get(symbol)

    def get_open_positions_count(self) -> int:
        return len(self._client.get_all_positions())

    def print_summary(self):
        acct = self._client.get_account()
        positions = self.get_positions()
        logger.info("=" * 50)
        logger.info(f"权益:      ${float(acct.equity):>12,.2f}")
        logger.info(f"现金:      ${float(acct.cash):>12,.2f}")
        logger.info(f"购买力:    ${float(acct.buying_power):>12,.2f}")
        logger.info(f"持仓数量:  {len(positions)}")
        for sym, pos in positions.items():
            pl_str = f"+{pos['unrealized_pl']:.2f}" if pos['unrealized_pl'] >= 0 \
                     else f"{pos['unrealized_pl']:.2f}"
            logger.info(
                f"  {sym:6s} qty={pos['qty']:.0f} "
                f"entry={pos['avg_entry']:.2f} "
                f"mv={pos['market_value']:.2f} "
                f"PnL={pl_str} ({float(pos['unrealized_plpc'])*100:.2f}%)"
            )
        logger.info("=" * 50)
