"""TSLA Wheel 策略 — 单次执行（供 GitHub Actions 调用）

每次运行：检查市场状态 → 检查 Wheel 阶段 → 执行一次 run_cycle() → 退出
由 GitHub Actions 每 5 分钟调用一次，无需本机保持开启。
"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from core.alpaca_client import AlpacaClients
from strategy.wheel_strategy import WheelStrategy
from loguru import logger

ET = ZoneInfo("America/New_York")


def main():
    now_et = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    logger.info(f"=== Wheel 单次检查 @ {now_et} ===")

    # 检查市场是否开盘
    try:
        clock = AlpacaClients.trading().get_clock()
    except Exception as e:
        logger.error(f"无法获取市场状态: {e}")
        sys.exit(1)

    if not clock.is_open:
        next_open = clock.next_open.astimezone(ET).strftime("%m/%d %H:%M ET")
        logger.info(f"市场休市，跳过本次。下次开盘: {next_open}")
        sys.exit(0)

    logger.info("市场开盘，执行 Wheel 策略检查...")
    try:
        WheelStrategy().run_cycle()
        logger.info("run_cycle() 完成")
    except Exception as e:
        logger.error(f"run_cycle() 异常: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
