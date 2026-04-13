"""Alpaca 客户端单例工厂 — 全项目共享同一批客户端实例"""
from __future__ import annotations
from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.trading.stream import TradingStream
from config import settings
from loguru import logger


class AlpacaClients:
    _trading:    TradingClient | None = None
    _historical: StockHistoricalDataClient | None = None
    _data_stream: StockDataStream | None = None
    _trade_stream: TradingStream | None = None

    @classmethod
    def trading(cls) -> TradingClient:
        if cls._trading is None:
            cls._trading = TradingClient(
                api_key=settings.API_KEY,
                secret_key=settings.SECRET_KEY,
                paper=settings.PAPER,
            )
            logger.info(f"TradingClient 初始化完成 (paper={settings.PAPER})")
        return cls._trading

    @classmethod
    def historical(cls) -> StockHistoricalDataClient:
        if cls._historical is None:
            cls._historical = StockHistoricalDataClient(
                api_key=settings.API_KEY,
                secret_key=settings.SECRET_KEY,
            )
            logger.info("StockHistoricalDataClient 初始化完成")
        return cls._historical

    @classmethod
    def data_stream(cls) -> StockDataStream:
        if cls._data_stream is None:
            cls._data_stream = StockDataStream(
                api_key=settings.API_KEY,
                secret_key=settings.SECRET_KEY,
            )
            logger.info("StockDataStream 初始化完成")
        return cls._data_stream

    @classmethod
    def trade_stream(cls) -> TradingStream:
        if cls._trade_stream is None:
            cls._trade_stream = TradingStream(
                api_key=settings.API_KEY,
                secret_key=settings.SECRET_KEY,
                paper=settings.PAPER,
            )
            logger.info("TradingStream 初始化完成")
        return cls._trade_stream
