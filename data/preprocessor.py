"""数据清洗与预处理"""
import pandas as pd
from loguru import logger


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """去重、排序、填充缺口"""
    df = df[~df.index.duplicated(keep="last")]
    df = df.sort_index()
    # 前向填充最多 5 根 K 线的缺口（节假日 / 停牌）
    df = df.ffill(limit=5)
    df = df.dropna()
    return df


def add_returns(df: pd.DataFrame) -> pd.DataFrame:
    """添加收益率列"""
    df = df.copy()
    df["returns"]    = df["close"].pct_change()
    df["log_returns"] = (df["close"] / df["close"].shift(1)).apply(
        lambda x: x if pd.isna(x) else __import__("math").log(x)
    )
    return df


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """确保列名小写、类型为 float"""
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = df[col].astype(float)
    return df


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """完整预处理流水线"""
    df = normalize_ohlcv(df)
    df = clean(df)
    df = add_returns(df)
    logger.debug(f"数据预处理完成，共 {len(df)} 行")
    return df
