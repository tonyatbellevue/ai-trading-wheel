"""TSLA Wheel 策略 — 单次执行（供 GitHub Actions 调用）

每次运行：检查市场状态 → 检查 Wheel 阶段 → 执行一次 run_cycle() → 退出
由 GitHub Actions 每 5 分钟调用一次，无需本机保持开启。

NOTE: Earlier versions piggybacked email_summary.py here for OPEN/CLOSE
window backup-trigger reliability. That was reverted on 5/1 — daily_sent_state.json
is per-runner local, so wheel.yml's 12 invocations per window each saw an
empty state and would fan out into 12 duplicate emails. wheel-summary.yml
is now the sole email trigger; if its cron drops, user manually fires
workflow_dispatch.

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

        # 1. Send a SIMPLE alert email immediately. Idempotency is implicit:
        #    set-difference (after - before) only fires on the SAME run that
        #    placed the order. Subsequent cycles see the order in `before`
        #    and `after`, so new_orders is empty for them. No cross-run
        #    state needed.
        try:
            from utils.emailer import send_email
            sgt = ZoneInfo("Asia/Singapore")
            now_et = datetime.now(ET).strftime("%H:%M ET")
            now_sgt = datetime.now(sgt).strftime("%H:%M SGT")

            # Pull the actual order objects to show details (side, qty, limit).
            order_lookup = {
                o.id: o for o in opt_mgr.get_open_option_orders(settings.WHEEL_SYMBOL)
            }
            order_lines = []
            for oid in new_orders:
                o = order_lookup.get(oid)
                if not o:
                    order_lines.append(f"  - order id {oid} (details unavailable)")
                    continue
                side = str(o.side).split(".")[-1]
                limit = float(o.limit_price) if o.limit_price else 0
                order_lines.append(
                    f"  • {side} {o.symbol} qty={o.qty} @ limit ${limit:.2f}"
                )

            subject = (f"[{settings.WHEEL_SYMBOL} Wheel] Trade alert "
                       f"— {now_et} (= {now_sgt})")
            body = (
                f"Bot placed {len(new_orders)} new option order(s):\n\n"
                + "\n".join(order_lines)
                + "\n\nFull summary will follow at next OPEN/CLOSE email."
            )
            ok = send_email(subject=subject, body_text=body)
            if ok:
                logger.info(f"📧 trade alert email sent: {subject}")
            else:
                logger.warning("trade alert email send returned False")
        except Exception as e:
            logger.warning(f"trade alert email failed: {e}")

        # 2. Existing path: write trade.flag + summary.md for the GitHub
        #    Actions workflow to create an Issue.
        try:
            from wheel_summary import build_summary
            md = build_summary("trade")
            with open("summary.md", "w", encoding="utf-8") as f:
                f.write(md)
            # 写标记文件
            with open("trade.flag", "w") as f:
                f.write("1")
            # 设置 GitHub Actions 输出
            gh_out = os.environ.get("GITHUB_OUTPUT")
            if gh_out:
                with open(gh_out, "a") as f:
                    f.write("traded=true\n")
        except Exception as e:
            logger.error(f"生成通知失败: {e}")


if __name__ == "__main__":
    main()
