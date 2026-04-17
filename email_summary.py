"""每日交易摘要邮件 — 开盘/收盘自动发送

用法：
    python email_summary.py open     # 发送开盘摘要
    python email_summary.py close    # 发送收盘摘要
    python email_summary.py          # 默认发送当前状态
"""
import sys
import os
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from wheel_summary import build_summary
from utils.emailer import send_email
from utils.dashboard_renderer import render_dashboard
from config import settings
from loguru import logger

ET = ZoneInfo("America/New_York")


def main(event: str = "update"):
    now_et = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    symbol = settings.WHEEL_SYMBOL

    event_labels = {
        "open":   "Market Open",
        "close":  "Market Close",
        "trade":  "Trade Alert",
        "update": "Status Update",
    }
    label = event_labels.get(event, event)

    logger.info(f"Generating {label} summary...")
    md = build_summary(event)

    subject = f"[{symbol} Wheel] {label} — {now_et}"
    html = render_dashboard(md)

    ok = send_email(subject=subject, body_text=md, body_html=html)
    if ok:
        logger.info(f"邮件已发送: {subject}")
    else:
        logger.error("邮件发送失败，请检查 .env 中的 EMAIL_* 配置")

    return ok


if __name__ == "__main__":
    ev = sys.argv[1] if len(sys.argv) > 1 else "update"
    main(ev)
