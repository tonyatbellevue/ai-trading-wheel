"""实时行情 WebSocket 订阅"""
from __future__ import annotations
import asyncio
import pandas as pd
from core.alpaca_client import AlpacaClients
from core.event_bus import bus, DataEvent
from config import settings
from loguru import logger


class LiveFeed:
    """订阅实时分钟 Bar，触发内部 DataEvent"""

    def __init__(self, symbols: list[str] = None):
        self.symbols = symbols or settings.SYMBOLS
        self._stream = AlpacaClients.data_stream()

    def _register_handlers(self):
        async def on_bar(bar):
            s = pd.Series({
                "open":   bar.open,
                "high":   bar.high,
                "low":    bar.low,
                "close":  bar.close,
                "volume": bar.volume,
            }, name=bar.timestamp)
            event = DataEvent(symbol=bar.symbol, bar=s)
            bus.publish(event)
            logger.debug(f"[LIVE] {bar.symbol} close={bar.close:.2f}")

        self._stream.subscribe_bars(on_bar, *self.symbols)

    def start(self):
        logger.info(f"启动实时行情订阅: {self.symbols}")
        self._register_handlers()
        self._stream.run()

    def stop(self):
        asyncio.run(self._stream.stop_ws())
        logger.info("实时行情订阅已停止")
