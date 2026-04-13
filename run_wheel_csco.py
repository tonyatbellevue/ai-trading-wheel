"""CSCO Wheel 期权策略持续监控入口

运行方式：python run_wheel_csco.py

状态机：
  IDLE → 卖 Cash-Secured Put
  SHORT_PUT → 等待到期/行权
  LONG_STOCK → 卖 Covered Call
  SHORT_CALL → 等待到期/行权
  循环...
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from config import settings
from core.alpaca_client import AlpacaClients
from strategy.wheel_strategy import WheelStrategy
from loguru import logger

ET = ZoneInfo("America/New_York")


def is_market_open() -> bool:
    try:
        return AlpacaClients.trading().get_clock().is_open
    except Exception as e:
        logger.warning(f"获取市场状态失败: {e}")
        return False


def main():
    logger.info("=" * 60)
    logger.info("  CSCO Option Wheel 策略监控启动")
    logger.info(f"  标的: {settings.WHEEL_SYMBOL}")
    logger.info(f"  目标 Delta: ±{settings.WHEEL_TARGET_DELTA}")
    logger.info(f"  DTE 范围: {settings.WHEEL_MIN_DTE}~{settings.WHEEL_MAX_DTE} 天")
    logger.info(f"  合约数量: {settings.WHEEL_CONTRACTS}")
    logger.info(f"  检查间隔: {settings.WHEEL_CHECK_INTERVAL} 秒")
    logger.info("=" * 60)

    strategy = WheelStrategy()
    interval = settings.WHEEL_CHECK_INTERVAL

    while True:
        now_et = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
        if is_market_open():
            logger.info(f"[{now_et}] 市场开盘 — 执行 Wheel 检查")
            try:
                strategy.run_cycle()
            except Exception as e:
                logger.error(f"run_cycle 异常: {e}")
        else:
            logger.info(f"[{now_et}] 市场休市 — 等待 {interval} 秒")
        time.sleep(interval)


if __name__ == "__main__":
    main()
