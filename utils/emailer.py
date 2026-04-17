"""邮件发送工具 — 通过 SMTP 发送交易摘要邮件

特性：
- 自动重试（最多 3 次，指数退避）
- 失败后写入本地待发送队列（reports/failed_emails/）
- 区分临时错误（网络）和致命错误（认证），分别处理
"""
import os
import smtplib
import ssl
import time
import json
from datetime import datetime
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from config import settings
from loguru import logger


FAILED_DIR = Path(settings.BASE_DIR) / "reports" / "failed_emails"
FAILED_DIR.mkdir(parents=True, exist_ok=True)


def _save_failed(subject: str, body_text: str, body_html: str | None, reason: str) -> Path:
    """把发送失败的邮件存到本地，便于后续手动/重试"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_subj = "".join(c if c.isalnum() else "_" for c in subject)[:50]
    fpath = FAILED_DIR / f"{ts}_{safe_subj}.json"
    fpath.write_text(json.dumps({
        "saved_at": datetime.now().isoformat(),
        "subject": subject,
        "body_text": body_text,
        "body_html": body_html,
        "failure_reason": reason,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return fpath


def _send_once(subject: str, body_text: str, body_html: str | None,
                server: str, port: int, sender: str, password: str,
                recipient: str) -> tuple[bool, str]:
    """单次发送尝试，返回 (是否成功, 错误信息)"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    if body_html:
        msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(server, port, timeout=30) as smtp:
            smtp.starttls(context=context)
            smtp.login(sender, password)
            smtp.sendmail(sender, recipient, msg.as_string())
        return True, ""
    except smtplib.SMTPAuthenticationError as e:
        return False, f"AUTH_ERROR: {e}"  # 致命：app password 错误
    except smtplib.SMTPRecipientsRefused as e:
        return False, f"RECIPIENT_REFUSED: {e}"  # 致命：收件人不存在
    except (smtplib.SMTPException, OSError, TimeoutError) as e:
        return False, f"NETWORK: {e}"  # 临时：可重试


def send_email(subject: str, body_text: str, body_html: str | None = None,
                max_retries: int = 3) -> bool:
    """发送邮件，失败自动重试

    Args:
        subject: 邮件标题
        body_text: 纯文本内容
        body_html: HTML 内容（可选，优先显示）
        max_retries: 最大重试次数（默认 3）

    Returns:
        True 发送成功, False 全部失败（已存到本地队列）
    """
    server = settings.EMAIL_SMTP_SERVER
    port = settings.EMAIL_SMTP_PORT
    sender = settings.EMAIL_SENDER
    password = settings.EMAIL_PASSWORD
    recipient = settings.EMAIL_RECIPIENT

    if not all([server, sender, password, recipient]):
        logger.error("邮件配置不完整，请检查 .env 中的 EMAIL_* 变量")
        _save_failed(subject, body_text, body_html, "config_incomplete")
        return False

    last_error = ""
    for attempt in range(1, max_retries + 1):
        ok, err = _send_once(subject, body_text, body_html,
                              server, port, sender, password, recipient)
        if ok:
            logger.info(f"邮件发送成功: {subject} → {recipient}"
                        f"{' (第 ' + str(attempt) + ' 次)' if attempt > 1 else ''}")
            return True

        last_error = err
        # 致命错误，不重试
        if err.startswith(("AUTH_ERROR", "RECIPIENT_REFUSED")):
            logger.error(f"邮件致命错误，不重试: {err}")
            break

        # 临时错误，指数退避重试 (2s, 4s, 8s...)
        if attempt < max_retries:
            wait = 2 ** attempt
            logger.warning(f"邮件发送失败 (第 {attempt} 次): {err}，{wait}s 后重试...")
            time.sleep(wait)

    # 全部失败 → 保存到本地队列
    fpath = _save_failed(subject, body_text, body_html, last_error)
    logger.error(f"邮件发送最终失败 ({max_retries} 次重试)，已保存至 {fpath}: {last_error}")
    return False


def retry_failed_emails(max_per_run: int = 5) -> dict:
    """重试本地队列中的失败邮件（可手动调用或定时任务调用）

    Returns:
        {"sent": N, "still_failed": N, "files_processed": [...]}
    """
    files = sorted(FAILED_DIR.glob("*.json"))[:max_per_run]
    result = {"sent": 0, "still_failed": 0, "files_processed": []}

    for fpath in files:
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
            ok = send_email(
                subject=f"[REPLAY] {data['subject']}",
                body_text=data["body_text"],
                body_html=data.get("body_html"),
                max_retries=2,
            )
            result["files_processed"].append(str(fpath.name))
            if ok:
                fpath.unlink()  # 成功后删除
                result["sent"] += 1
            else:
                result["still_failed"] += 1
        except Exception as e:
            logger.error(f"重试失败邮件 {fpath} 出错: {e}")
            result["still_failed"] += 1
    return result


def md_to_html(md: str) -> str:
    """将 Markdown 摘要转为简单 HTML 邮件格式"""
    import re

    lines = md.split("\n")
    html_lines = []
    in_table = False

    for line in lines:
        stripped = line.strip()

        # 表格行
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            # 跳过分隔行
            if all(set(c) <= {"-", ":", " "} for c in cells):
                continue
            if not in_table:
                html_lines.append('<table style="border-collapse:collapse;width:100%;margin:8px 0;">')
                tag = "th"
                in_table = True
            else:
                tag = "td"
            row = "".join(
                f'<{tag} style="border:1px solid #ddd;padding:6px 10px;text-align:left;">{c}</{tag}>'
                for c in cells
            )
            html_lines.append(f"<tr>{row}</tr>")
            continue

        if in_table:
            html_lines.append("</table>")
            in_table = False

        if not stripped:
            html_lines.append("<br>")
        elif stripped.startswith("# "):
            html_lines.append(f'<h2 style="color:#1a73e8;border-bottom:2px solid #1a73e8;padding-bottom:4px;">{stripped[2:]}</h2>')
        elif stripped.startswith("## "):
            html_lines.append(f'<h3 style="color:#333;margin-top:16px;">{stripped[3:]}</h3>')
        elif stripped.startswith("### "):
            html_lines.append(f'<h4 style="color:#555;">{stripped[4:]}</h4>')
        elif stripped.startswith("> "):
            html_lines.append(f'<blockquote style="border-left:3px solid #ccc;padding-left:10px;color:#666;">{stripped[2:]}</blockquote>')
        elif stripped.startswith("- "):
            html_lines.append(f"<li>{stripped[2:]}</li>")
        else:
            # 加粗和代码
            text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", stripped)
            text = re.sub(r"`(.+?)`", r'<code style="background:#f0f0f0;padding:1px 4px;border-radius:3px;">\1</code>', text)
            html_lines.append(f"<p style='margin:4px 0;'>{text}</p>")

    if in_table:
        html_lines.append("</table>")

    body = "\n".join(html_lines)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Segoe UI,Arial,sans-serif;max-width:800px;margin:auto;padding:16px;color:#222;">
{body}
<hr style="margin-top:24px;border:none;border-top:1px solid #ddd;">
<p style="color:#999;font-size:12px;">AI Trading System — 自动生成</p>
</body></html>"""
