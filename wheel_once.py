"""TSLA Wheel 策略 — 单次执行（供 GitHub Actions 调用）

每次运行：检查市场状态 → 检查 Wheel 阶段 → 执行一次 run_cycle() → 退出
由 GitHub Actions 每 5 分钟调用一次，无需本机保持开启。

也兼任开市/收市邮件的 fallback 触发器：wheel-summary.yml 的 cron 在 GHA
free tier 经常被延迟 1-2 小时甚至丢失，wheel.yml 这个 5 分钟 cron 命中率
高得多，顺便检查"该发的邮件发了没"。idempotency 由 email_summary.py 的
daily_sent_state.json 保证，所以 wheel-summary.yml + wheel.yml 双触发也
不会重复发。

输出变量：
- 如果下单，会写 trade.flag 文件 + 写 summary.md 供工作流发 Issue
"""
import sys
import os
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from core.alpaca_client import AlpacaClients
from strategy.wheel_strategy import WheelStrategy, WheelPhase
from execution.option_order_manager import OptionOrderManager
from config import settings
from loguru import logger

ET = ZoneInfo("America/New_York")


def _maybe_send_scheduled_email() -> None:
    """If we're inside the OPEN or CLOSE email window and that email hasn't
    been sent today, fire email_summary.py. Idempotency lives in the
    target script, so it's safe to call this every 5 minutes.

    Windows are deliberately wide (1 hour) so that even if wheel.yml itself
    is slightly delayed by GHA, the email still goes out.
    """
    now_et = datetime.now(ET)
    minutes = now_et.hour * 60 + now_et.minute

    # ET 09:30 → 10:30  (OPEN window)
    # ET 16:00 → 17:00  (CLOSE window)
    if 9 * 60 + 30 <= minutes <= 10 * 60 + 30:
        event = "open"
    elif 16 * 60 <= minutes <= 17 * 60:
        event = "close"
    else:
        return

    try:
        # subprocess so we get a clean Python process — avoids any state the
        # wheel cycle may have left behind (Alpaca client caches, env, etc.)
        result = subprocess.run(
            [sys.executable, "email_summary.py", event],
            capture_output=True, text=True, timeout=180,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        if result.returncode == 0:
            logger.info(f"📧 piggyback {event} email check OK")
        else:
            logger.warning(
                f"📧 piggyback {event} email failed (rc={result.returncode}): "
                f"{(result.stderr or result.stdout)[:400]}"
            )
    except subprocess.TimeoutExpired:
        logger.warning(f"📧 piggyback {event} email timeout (180s)")
    except Exception as e:
        logger.warning(f"📧 piggyback {event} email exception: {e}")


def main():
    now_et = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    logger.info(f"=== Wheel 单次检查 @ {now_et} ===")

    # 检查市场是否开盘
    try:
        clock = AlpacaClients.trading().get_clock()
    except Exception as e:
        logger.error(f"无法获取市场状态: {e}")
        sys.exit(1)

    # Always try the email piggyback — runs in OPEN/CLOSE windows regardless
    # of market state (the CLOSE email needs to fire right after the bell).
    _maybe_send_scheduled_email()

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
        # 生成 summary 供工作流使用
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
