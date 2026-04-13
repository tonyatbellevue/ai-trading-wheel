"""回测入口 — 策略历史验证"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data.historical_feed import fetch_bars
from data.preprocessor import prepare
from backtest.engine import BacktestEngine
from backtest.report_generator import generate_report
from models.rf_classifier import RFClassifier
from config import settings
from loguru import logger


def main(use_ml: bool = True, symbols: list = None, start: str = None, end: str = None):
    symbols = symbols or settings.SYMBOLS
    start   = start   or settings.BACKTEST_START
    end     = end     or settings.BACKTEST_END

    logger.info(f"回测配置: {symbols} | {start} ~ {end} | ML={'开' if use_ml else '关'}")

    # 拉取历史数据
    raw_data = fetch_bars(symbols=symbols, start=start, end=end,
                          timeframe=settings.BACKTEST_TIMEFRAME)

    # 数据预处理
    data = {sym: prepare(df) for sym, df in raw_data.items()}

    # 加载 ML 模型（可选）
    model = None
    if use_ml:
        model = RFClassifier()
        try:
            model.load()
        except FileNotFoundError:
            logger.warning("未找到训练好的模型，先运行: python train_model.py")
            logger.warning("将以纯规则模式运行（无 ML 过滤）")
            model = None

    # 运行回测
    engine = BacktestEngine(model=model)
    metrics = engine.run(data)

    # 生成报告
    report_path = generate_report(engine.broker, metrics)
    logger.info(f"报告已生成，请用浏览器打开: {report_path}")

    return metrics


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="AI 交易回测")
    parser.add_argument("--no-ml", action="store_true", help="禁用 ML 过滤，使用纯规则模式")
    parser.add_argument("--symbols", nargs="+", default=None, help="交易标的列表")
    parser.add_argument("--start",   type=str, default=None, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end",     type=str, default=None, help="结束日期 YYYY-MM-DD")
    args = parser.parse_args()

    main(
        use_ml=not args.no_ml,
        symbols=args.symbols,
        start=args.start,
        end=args.end,
    )
