"""每日交易摘要邮件 — 开盘/收盘自动发送

用法：
    python email_summary.py open     # 发送开盘摘要
    python email_summary.py close    # 发送收盘摘要
    python email_summary.py          # 默认发送当前状态

幂等语义（workflow 冗余 cron 用）：
  - 每个 (date, event) 组合只发一封；后续调用看到 sent-state 文件就静默退出。
  - 设置 WHEEL_EMAIL_FORCE=1 强制发送（手动触发 / workflow_dispatch 用）。
"""
import sys
import os
import json
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

from wheel_summary import build_summary
from utils.emailer import send_email
from utils.dashboard_renderer import render_dashboard
from config import settings
from loguru import logger

ET = ZoneInfo("America/New_York")

SENT_STATE_FILE = Path(settings.BASE_DIR) / "reports" / "daily_sent_state.json"


def _already_sent_today(event: str) -> bool:
    """Return True if we've already sent this event's email today (in ET).

    Uses ET date so an 'open' at 09:31 ET and a 'close' at 16:05 ET same
    day are both correctly scoped.
    """
    if not SENT_STATE_FILE.exists():
        return False
    try:
        state = json.loads(SENT_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return False
    key = f"{date.today().isoformat()}:{event}"
    # Allow manual date key in ET
    et_date = datetime.now(ET).date().isoformat()
    return state.get(f"{et_date}:{event}") is True or state.get(key) is True


def _mark_sent(event: str) -> None:
    SENT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(SENT_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {}

    # Prune old entries (> 14 days) so the file doesn't grow forever
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=14)).isoformat()
    state = {k: v for k, v in state.items() if k.split(":", 1)[0] >= cutoff}

    et_date = datetime.now(ET).date().isoformat()
    state[f"{et_date}:{event}"] = True
    state[f"{et_date}:{event}:ts"] = datetime.now().isoformat()
    SENT_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main(event: str = "update"):
    force = os.environ.get("WHEEL_EMAIL_FORCE", "").strip() == "1"
    if not force and _already_sent_today(event):
        logger.info(f"📭 Today's {event} email was already sent — skipping "
                    f"(set WHEEL_EMAIL_FORCE=1 to override).")
        return True   # treat as success so workflow passes

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
        if event in ("open", "close") and not force:
            _mark_sent(event)
    else:
        logger.error("邮件发送失败，请检查 .env 中的 EMAIL_* 配置")

    return ok


if __name__ == "__main__":
    ev = sys.argv[1] if len(sys.argv) > 1 else "update"
    ok = main(ev)
    sys.exit(0 if ok else 1)
