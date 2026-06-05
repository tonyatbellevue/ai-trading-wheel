"""Phase 2 (P2.4): consecutive-skip monitor for SL/TP decisions.

When _get_option_fair_price returns None, the bot skips stop-loss /
take-profit for that position this cycle. That's the SAFE behavior — but
if a quote stays broken for a long time, the position is effectively
unmonitored and the operator should know.

This tracks consecutive skips per option symbol across cron runs (state
persisted to JSON since each wheel_once.py invocation is a fresh process).
After ALERT_THRESHOLD consecutive skips (~10 ticks ≈ 80 min at 8-min cron),
it sends ONE email alert, then throttles further alerts for that symbol.

A successful fair-price read (record_ok) resets the counter to 0.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from config import settings

STATE_FILE = Path(settings.BASE_DIR) / "metrics" / "data" / "skip_monitor.json"
ALERT_THRESHOLD = 10        # consecutive skips before first alert
ALERT_REPEAT_EVERY = 20     # re-alert every N additional skips after first


def _load() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug(f"skip_monitor load failed: {e}")
    return {}


def _save(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug(f"skip_monitor save failed: {e}")


def record_ok(symbol: str) -> None:
    """A reliable quote was obtained — reset the skip counter."""
    state = _load()
    if symbol in state and state[symbol].get("count", 0) > 0:
        logger.debug(f"skip_monitor: {symbol} quote recovered, reset counter")
        state[symbol] = {"count": 0, "last_alert_at": 0}
        _save(state)


def record_skip(symbol: str, reason: str = "no reliable quote") -> None:
    """A SL/TP decision was skipped due to bad quote. Increment counter,
    send email alert at threshold (and periodically thereafter)."""
    state = _load()
    entry = state.get(symbol, {"count": 0, "last_alert_at": 0})
    entry["count"] = entry.get("count", 0) + 1
    count = entry["count"]
    last_alert = entry.get("last_alert_at", 0)

    should_alert = (
        count == ALERT_THRESHOLD or
        (count > ALERT_THRESHOLD and (count - last_alert) >= ALERT_REPEAT_EVERY)
    )
    if should_alert:
        _send_alert(symbol, count, reason)
        entry["last_alert_at"] = count

    state[symbol] = entry
    _save(state)
    if count >= ALERT_THRESHOLD:
        logger.warning(f"⚠️ skip_monitor: {symbol} 连续 {count} 次跳过 SL/TP ({reason})")


def _send_alert(symbol: str, count: int, reason: str) -> None:
    try:
        from utils.emailer import send_email
        from zoneinfo import ZoneInfo
        et = datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M ET")
        sgt = datetime.now(ZoneInfo("Asia/Singapore")).strftime("%H:%M SGT")
        mins = count * 8  # approx, 8-min cron
        subject = f"[Wheel ⚠️] {symbol} 报价异常 — 已连续 {count} 次跳过风控"
        body = (
            f"标的 {symbol} 的止损/止盈判断已连续 {count} 次被跳过 "
            f"(~{mins} 分钟)。\n\n"
            f"原因: {reason}\n"
            f"时间: {et} (= {sgt})\n\n"
            f"含义: 该仓位的自动风控暂时失效 (拿不到可靠报价)。\n"
            f"这是设计上的安全行为 (宁可不动也不用错误报价乱平仓), \n"
            f"但若报价长期损坏, 该仓位实际处于无监控状态。\n\n"
            f"建议: 手动检查 {symbol} 期权链报价是否正常, 必要时人工处理。\n"
            f"(本邮件为只读告警, bot 不会自动下单。)"
        )
        ok = send_email(subject=subject, body_text=body)
        if ok:
            logger.warning(f"📧 skip_monitor alert sent: {symbol} x{count}")
    except Exception as e:
        logger.warning(f"skip_monitor 告警邮件失败: {e}")
