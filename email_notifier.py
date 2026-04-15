"""Email 通知模块 — 替代 GitHub Issues 通知

用法：
    from email_notifier import send
    send("[Trade] TSLA Wheel — 2026-04-15", markdown_body)

NOTIFY_EMAIL 未设置时静默跳过，不影响主流程。
Gmail 需要使用「应用专用密码」（App Password），不是账户密码。
获取方式：Google 账户 → 安全性 → 两步验证 → 应用专用密码
"""
from __future__ import annotations

import smtplib
import re
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

from config import settings
from loguru import logger


def _md_to_html(md: str) -> str:
    """把 Markdown 转成简单 HTML（优先用 markdown 库，否则用 <pre> 包裹）"""
    try:
        import markdown as md_lib
        body = md_lib.markdown(md, extensions=["tables", "nl2br"])
    except ImportError:
        # 降级：原样包在 <pre> 里，至少可读
        escaped = md.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        body = f"<pre style='font-family:monospace;font-size:14px'>{escaped}</pre>"

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         max-width: 900px; margin: 40px auto; padding: 0 20px; color: #333; }}
  h1,h2,h3 {{ color: #1a1a2e; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  th,td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
  th {{ background: #f4f4f8; }}
  code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 3px; }}
  pre  {{ background: #f6f8fa; padding: 16px; border-radius: 6px; overflow-x: auto; }}
  hr   {{ border: none; border-top: 1px solid #eee; margin: 24px 0; }}
  .footer {{ color: #888; font-size: 12px; margin-top: 32px; }}
</style>
</head>
<body>
{body}
<p class="footer">发送时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
</body>
</html>"""


def send(subject: str, markdown_body: str) -> bool:
    """发送 HTML 邮件。返回 True 成功，False 失败/跳过。"""
    if not settings.NOTIFY_EMAIL:
        return False
    if not settings.SMTP_USER or not settings.SMTP_PASS:
        logger.warning("Email: SMTP_USER / SMTP_PASS 未配置，跳过发送")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.SMTP_USER
        msg["To"] = settings.NOTIFY_EMAIL

        # 纯文本备用
        msg.attach(MIMEText(markdown_body, "plain", "utf-8"))
        # HTML 正文
        msg.attach(MIMEText(_md_to_html(markdown_body), "html", "utf-8"))

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(settings.SMTP_USER, settings.SMTP_PASS)
            smtp.sendmail(settings.SMTP_USER, settings.NOTIFY_EMAIL, msg.as_string())

        logger.info(f"✉️  邮件已发送 → {settings.NOTIFY_EMAIL} | {subject}")
        return True

    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False


def send_trade_alert(markdown_body: str) -> bool:
    """交易通知邮件"""
    from datetime import date
    subject = f"[Trade] {settings.WHEEL_SYMBOL} Wheel — {date.today()}"
    return send(subject, markdown_body)


def send_summary(event: str, markdown_body: str) -> bool:
    """每日开盘/收盘摘要邮件"""
    from datetime import date
    label = {"open": "Open", "close": "Close"}.get(event, "Update")
    subject = f"[{label}] {settings.WHEEL_SYMBOL} Wheel — {date.today()}"
    return send(subject, markdown_body)
