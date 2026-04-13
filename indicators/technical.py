"""技术指标计算 — 基于 ta 库（兼容 Python 3.14+）"""
import pandas as pd
from ta.trend import EMAIndicator, SMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange, BollingerBands
from config import settings


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]

    # ── EMA / SMA ────────────────────────────────────────────────────
    df[f"ema_{settings.EMA_SHORT}"] = EMAIndicator(close, window=settings.EMA_SHORT).ema_indicator()
    df[f"ema_{settings.EMA_LONG}"]  = EMAIndicator(close, window=settings.EMA_LONG).ema_indicator()
    df[f"sma_{settings.SMA_FILTER}"] = SMAIndicator(close, window=settings.SMA_FILTER).sma_indicator()
    df["sma_200"] = SMAIndicator(close, window=200).sma_indicator()

    # ── RSI ──────────────────────────────────────────────────────────
    df["rsi"] = RSIIndicator(close, window=settings.RSI_PERIOD).rsi()

    # ── MACD ─────────────────────────────────────────────────────────
    macd_obj = MACD(close,
                    window_fast=settings.MACD_FAST,
                    window_slow=settings.MACD_SLOW,
                    window_sign=settings.MACD_SIGNAL)
    df["macd"]        = macd_obj.macd()
    df["macd_signal"] = macd_obj.macd_signal()
    df["macd_hist"]   = macd_obj.macd_diff()

    # ── ATR ──────────────────────────────────────────────────────────
    df["atr"] = AverageTrueRange(high, low, close, window=settings.ATR_PERIOD).average_true_range()

    # ── Bollinger Bands ───────────────────────────────────────────────
    bb = BollingerBands(close, window=20, window_dev=2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_mid"]   = bb.bollinger_mavg()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / (df["bb_mid"] + 1e-9)

    # ── EMA 交叉信号 ──────────────────────────────────────────────────
    ema_s = df[f"ema_{settings.EMA_SHORT}"]
    ema_l = df[f"ema_{settings.EMA_LONG}"]
    df["ema_cross_up"]   = (ema_s > ema_l) & (ema_s.shift(1) <= ema_l.shift(1))
    df["ema_cross_down"] = (ema_s < ema_l) & (ema_s.shift(1) >= ema_l.shift(1))

    return df
