"""本地调度主入口 — 替代 GitHub Actions

用法：
    python run_local.py          # 正式运行（保持终端开着）
    python run_local.py --test   # 立刻跑一次 open summary，不等 cron 时间

调度逻辑（America/New_York，周一~五）：
    09:30  → wheel_summary open  + 邮件
    09:35, 09:40 ... 15:55 每5分钟 → wheel_once（Alpaca 交易周期）
    16:05  → wheel_summary close + 邮件
"""
import sys
import signal
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

ET = ZoneInfo("America/New_York")


# ── 任务函数 ──────────────────────────────────────────────────────────────────

def job_wheel_cycle():
    """每 5 分钟：运行一次 Wheel 交易周期（和 wheel_once.py main() 相同）"""
    try:
        from wheel_once import main as wheel_once_main
        wheel_once_main()
    except SystemExit:
        pass   # wheel_once.py 会 sys.exit(0) 在市场休市时，这里正常忽略
    except Exception as e:
        logger.error(f"[wheel_cycle] 异常: {e}")


def job_summary(event: str):
    """开盘/收盘：生成摘要 + 发邮件"""
    try:
        from wheel_summary import build_summary
        from email_notifier import send_summary
        logger.info(f"[summary:{event}] 开始生成摘要...")
        md = build_summary(event)
        print(md)
        send_summary(event, md)
        logger.info(f"[summary:{event}] 完成")
    except Exception as e:
        logger.error(f"[summary:{event}] 异常: {e}")


# ── 调度配置 ──────────────────────────────────────────────────────────────────

def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=ET)

    # 09:30 开盘摘要（周一~五）
    scheduler.add_job(
        lambda: job_summary("open"),
        CronTrigger(day_of_week="mon-fri", hour=9, minute=30, timezone=ET),
        id="open_summary",
        name="Market Open Summary",
    )

    # 09:35 ~ 15:55，每 5 分钟运行一次 Wheel 交易周期（周一~五）
    scheduler.add_job(
        job_wheel_cycle,
        CronTrigger(day_of_week="mon-fri", hour="9-15", minute="35,40,45,50,55,0,5,10,15,20,25,30", timezone=ET),
        id="wheel_cycle",
        name="Wheel Trade Cycle (every 5 min)",
    )

    # 16:05 收盘摘要（周一~五）
    scheduler.add_job(
        lambda: job_summary("close"),
        CronTrigger(day_of_week="mon-fri", hour=16, minute=5, timezone=ET),
        id="close_summary",
        name="Market Close Summary",
    )

    return scheduler


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main():
    test_mode = "--test" in sys.argv

    now_et = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    logger.info("=" * 60)
    logger.info(f"  Wheel 本地调度器启动 @ {now_et}")
    logger.info("=" * 60)

    if test_mode:
        logger.info("[TEST MODE] 立刻运行一次 open summary，然后退出")
        job_summary("open")
        logger.info("[TEST MODE] 完成")
        return

    scheduler = build_scheduler()

    # 优雅退出
    def _shutdown(sig, frame):
        logger.info("收到停止信号，正在关闭调度器...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("调度任务已注册：")
    for job in scheduler.get_jobs():
        logger.info(f"  [{job.id}] {job.name}")
    logger.info("按 Ctrl+C 停止")
    logger.info("-" * 60)

    scheduler.start()


if __name__ == "__main__":
    main()
