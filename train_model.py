"""ML 模型训练入口 — 首次使用前运行一次"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data.historical_feed import fetch_bars
from data.preprocessor import prepare
from indicators.technical import add_all_indicators
from models.rf_classifier import RFClassifier
from config import settings
from loguru import logger

import pandas as pd


def main():
    logger.info("开始训练 ML 模型...")
    logger.info(f"训练标的: {settings.SYMBOLS}")
    logger.info(f"数据范围: {settings.BACKTEST_START} ~ {settings.BACKTEST_END}")

    # 拉取历史数据（日线，用于训练）
    raw_data = fetch_bars(
        symbols=settings.SYMBOLS,
        start=settings.BACKTEST_START,
        end=settings.BACKTEST_END,
        timeframe=settings.BACKTEST_TIMEFRAME,
    )

    # 合并所有标的数据
    all_dfs = []
    for sym, df in raw_data.items():
        df = prepare(df)
        df = add_all_indicators(df)
        df["symbol"] = sym
        all_dfs.append(df)

    combined = pd.concat(all_dfs)
    combined = combined.sort_index()
    logger.info(f"合并数据集: {len(combined)} 行")

    # 训练模型
    model = RFClassifier()
    report = model.train(combined)

    logger.info("模型训练完成！")
    logger.info(f"验证集精准率 (UP): {report.get('1', {}).get('precision', 0):.3f}")
    logger.info(f"验证集精准率 (DOWN): {report.get('-1', {}).get('precision', 0):.3f}")
    logger.info(f"模型已保存至: {RFClassifier.MODEL_FILE}")


if __name__ == "__main__":
    main()
