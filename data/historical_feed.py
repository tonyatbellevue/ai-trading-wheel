"""历史 K 线数据拉取与本地缓存"""
from __future__ import annotations
import os
import pandas as pd
from datetime import datetime
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from core.alpaca_client import AlpacaClients
from core.exceptions import DataFetchError
from config import settings
from loguru import logger


def _parse_timeframe(tf: str) -> TimeFrame:
    mapping = {
        "1Min":  TimeFrame(1,  TimeFrameUnit.Minute),
        "5Min":  TimeFrame(5,  TimeFrameUnit.Minute),
        "15Min": TimeFrame(15, TimeFrameUnit.Minute),
        "1Hour": TimeFrame(1,  TimeFrameUnit.Hour),
        "1Day":  TimeFrame(1,  TimeFrameUnit.Day),
    }
    if tf not in mapping:
        raise ValueError(f"不支持的时间粒度: {tf}，可选: {list(mapping.keys())}")
    return mapping[tf]


def fetch_bars(
    symbols: list[str],
    start: str,
    end: str,
    timeframe: str = settings.BACKTEST_TIMEFRAME,
    use_cache: bool = True,
) -> dict[str, pd.DataFrame]:
    """
    拉取多标的历史 K 线，返回 {symbol: DataFrame}。
    DataFrame 列: open, high, low, close, volume, timestamp (index)
    """
    os.makedirs(settings.DATA_CACHE, exist_ok=True)
    result: dict[str, pd.DataFrame] = {}

    for symbol in symbols:
        cache_path = os.path.join(
            settings.DATA_CACHE,
            f"{symbol}_{timeframe}_{start}_{end}.parquet"
        )
        if use_cache and os.path.exists(cache_path):
            df = pd.read_parquet(cache_path)
            logger.info(f"[{symbol}] 从缓存加载 {len(df)} 根 K 线")
            result[symbol] = df
            continue

        try:
            client = AlpacaClients.historical()
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=_parse_timeframe(timeframe),
                start=datetime.fromisoformat(start),
                end=datetime.fromisoformat(end),
                adjustment="all",
            )
            bars = client.get_stock_bars(req)
            df = bars.df

            if isinstance(df.index, pd.MultiIndex):
                df = df.xs(symbol, level="symbol")

            df.index = pd.to_datetime(df.index, utc=True)
            df.index = df.index.tz_convert("America/New_York")
            df = df[["open", "high", "low", "close", "volume"]].copy()
            df.to_parquet(cache_path)
            logger.info(f"[{symbol}] 拉取 {len(df)} 根 K 线 ({start} ~ {end})")
            result[symbol] = df

        except Exception as e:
            logger.error(f"[{symbol}] 数据拉取失败: {e}")
            raise DataFetchError(f"{symbol}: {e}") from e

    return result


def fetch_single(
    symbol: str,
    start: str,
    end: str,
    timeframe: str = settings.BACKTEST_TIMEFRAME,
) -> pd.DataFrame:
    """单标的历史数据快捷方法"""
    return fetch_bars([symbol], start, end, timeframe)[symbol]
