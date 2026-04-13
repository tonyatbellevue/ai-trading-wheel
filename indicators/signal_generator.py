"""规则型信号生成器 — EMA 交叉 + RSI 过滤"""
import pandas as pd
from config import settings


def generate_rule_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    在已含指标的 DataFrame 上生成规则信号列。
    rule_signal: 1=做多, -1=做空, 0=持有
    """
    df = df.copy()
    ema_s = f"ema_{settings.EMA_SHORT}"
    ema_l = f"ema_{settings.EMA_LONG}"
    sma_f = f"sma_{settings.SMA_FILTER}"

    # 做多条件
    long_cond = (
        df["ema_cross_up"] &                                          # EMA 金叉
        (df["close"] > df[sma_f]) &                                   # 大趋势向上
        (df["rsi"] >= settings.RSI_ENTRY_MIN) &                       # RSI 非超卖
        (df["rsi"] <= settings.RSI_ENTRY_MAX) &                       # RSI 非超买
        (df["macd_hist"] > 0)                                         # MACD 动量正
    )

    # 做空条件（平多 / 开空）
    short_cond = (
        df["ema_cross_down"] &                                        # EMA 死叉
        (df["close"] < df[sma_f]) &                                   # 大趋势向下
        (df["rsi"] >= (100 - settings.RSI_ENTRY_MAX)) &               # RSI 非超买
        (df["rsi"] <= (100 - settings.RSI_ENTRY_MIN)) &               # RSI 非超卖
        (df["macd_hist"] < 0)                                         # MACD 动量负
    )

    df["rule_signal"] = 0
    df.loc[long_cond,  "rule_signal"] = 1
    df.loc[short_cond, "rule_signal"] = -1

    # 超买强制减仓标记
    df["force_exit"] = (
        (df["rsi"] > settings.RSI_OVERBOUGHT) |
        (df["rsi"] < settings.RSI_OVERSOLD)
    )

    return df
