"""ML 特征工程"""
import pandas as pd
import numpy as np
from config import settings


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    构建机器学习特征矩阵。
    所有特征均使用过去数据，无前视偏差。
    """
    df = df.copy()
    ema_s = f"ema_{settings.EMA_SHORT}"
    ema_l = f"ema_{settings.EMA_LONG}"
    sma_f = f"sma_{settings.SMA_FILTER}"

    # ── 价格偏离特征 ──────────────────────────────────────────────────
    df["feat_close_ema_s_ratio"] = df["close"] / df[ema_s] - 1
    df["feat_close_ema_l_ratio"] = df["close"] / df[ema_l] - 1
    df["feat_close_sma_ratio"]   = df["close"] / df[sma_f] - 1
    df["feat_ema_diff"]          = df[ema_s] / df[ema_l] - 1

    # ── 动量特征 ─────────────────────────────────────────────────────
    for n in [1, 3, 5, 10, 20]:
        df[f"feat_ret_{n}"] = df["close"].pct_change(n)

    # ── 指标特征 ─────────────────────────────────────────────────────
    df["feat_rsi"]        = df["rsi"] / 100.0
    df["feat_macd_hist"]  = df["macd_hist"]
    df["feat_bb_width"]   = df["bb_width"]
    df["feat_atr_ratio"]  = df["atr"] / df["close"]

    # ── 波动率特征 ───────────────────────────────────────────────────
    df["feat_vol_5"]  = df["returns"].rolling(5).std()
    df["feat_vol_20"] = df["returns"].rolling(20).std()
    df["feat_vol_ratio"] = df["feat_vol_5"] / (df["feat_vol_20"] + 1e-9)

    # ── 成交量特征 ───────────────────────────────────────────────────
    df["feat_vol_sma_ratio"] = df["volume"] / df["volume"].rolling(20).mean()

    # ── 滞后特征 ─────────────────────────────────────────────────────
    df["feat_rsi_lag1"] = df["feat_rsi"].shift(1)
    df["feat_rsi_lag3"] = df["feat_rsi"].shift(3)

    return df


def build_labels(df: pd.DataFrame,
                 horizon: int = settings.ML_LABEL_HORIZON,
                 threshold: float = settings.ML_LABEL_THRESHOLD) -> pd.DataFrame:
    """
    构建三分类标签:
      1  = 未来 N 根 K 线上涨超过阈值
     -1  = 未来 N 根 K 线下跌超过阈值
      0  = 横盘
    """
    df = df.copy()
    future_ret = df["close"].shift(-horizon) / df["close"] - 1
    df["label"] = 0
    df.loc[future_ret >  threshold, "label"] =  1
    df.loc[future_ret < -threshold, "label"] = -1
    return df


FEATURE_COLS = [c for c in [
    "feat_close_ema_s_ratio", "feat_close_ema_l_ratio", "feat_close_sma_ratio",
    "feat_ema_diff",
    "feat_ret_1", "feat_ret_3", "feat_ret_5", "feat_ret_10", "feat_ret_20",
    "feat_rsi", "feat_macd_hist", "feat_bb_width", "feat_atr_ratio",
    "feat_vol_5", "feat_vol_20", "feat_vol_ratio", "feat_vol_sma_ratio",
    "feat_rsi_lag1", "feat_rsi_lag3",
]]
