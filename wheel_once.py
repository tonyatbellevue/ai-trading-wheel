"""TSLA Wheel 策略 — 单次执行（供 GitHub Actions 调用）

每次运行：检查市场状态 → 检查 Wheel 阶段 → 执行一次 run_cycle() → 退出
由 GitHub Actions 每 5 分钟调用一次，无需本机保持开启。

输出变量：
- 如果下单，会写 trade.flag 文件 + 写 summary.md 供工作流发 Issue
"""
import sys
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from core.alpaca_client import AlpacaClients
from strategy.wheel_strategy import WheelStrategy, WheelPhase
from execution.option_order_manager import OptionOrderManager
from config import settings
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

    # 记录执行前的订单数，用于检测是否新下单
    opt_mgr = OptionOrderManager()
    before_orders = {o.id for o in opt_mgr.get_open_option_orders(settings.WHEEL_SYMBOL)}

    try:
        strategy = WheelStrategy()
        phase_before, _ = strategy.get_phase()
        strategy.run_cycle()
        logger.info("run_cycle() 完成")
    except Exception as e:
        logger.error(f"run_cycle() 异常: {e}")
        sys.exit(1)

    # 检测是否有新下单
    after_orders = {o.id for o in opt_mgr.get_open_option_orders(settings.WHEEL_SYMBOL)}
    new_orders = after_orders - before_orders

    if new_orders:
        logger.info(f"检测到 {len(new_orders)} 个新下单 → 生成交易通知")
        try:
            from wheel_summary import build_summary
            md = build_summary("trade")
            # 本地 Email 通知
            if settings.NOTIFY_EMAIL:
                from email_notifier import send_trade_alert
                send_trade_alert(md)
            # GitHub Actions 兼容（本地跑时 GITHUB_OUTPUT 不存在，自动跳过）
            gh_out = os.environ.get("GITHUB_OUTPUT")
            if gh_out:
                with open(gh_out, "a") as f:
                    f.write("traded=true\n")
        except Exception as e:
            logger.error(f"生成通知失败: {e}")


if __name__ == "__main__":
    main()
