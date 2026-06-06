"""Phase 4.4 — partial-assignment (<100 shares) email alert.

When the bot ends up holding 1-99 shares of a symbol (partial fill or odd
assignment), it can't sell a covered call (needs 100) and won't sell a new
put (it's in LONG_STOCK), so the position just sits — managed only by the
-35% stop. The operator should be told to top up to 100 or clear the lot.

get_phase() runs several times per cron cycle, so this throttles to ONE
email per symbol per day (state in JSON). reset() clears once the holding
is no longer partial (back to 0 or ≥100).
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from loguru import logger
from config import settings

STATE_FILE = Path(settings.BASE_DIR) / "metrics" / "data" / "partial_alert.json"


def _load() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug(f"partial_alert save failed: {e}")


def reset(symbol: str) -> None:
    """Clear alert state once the holding is no longer partial."""
    state = _load()
    if symbol in state:
        state.pop(symbol, None)
        _save(state)


def maybe_alert(symbol: str, qty: float, cost_basis: float) -> None:
    """Send ONE email per symbol per day about a partial (<100 sh) holding."""
    today = date.today().isoformat()
    state = _load()
    if state.get(symbol, {}).get("last_alert_date") == today:
        return  # already alerted today

    try:
        from utils.emailer import send_email
        et = datetime.now(ZoneInfo("America/New_York")).strftime("%m/%d %H:%M ET")
        sgt = datetime.now(ZoneInfo("Asia/Singapore")).strftime("%m/%d %H:%M SGT")
        need = 100 - int(qty)
        subject = f"[Wheel ⚠️] {symbol} 零头持仓 {int(qty)} 股 (不足 100, 需人工处理)"
        body = (
            f"标的 {symbol} 当前只有 {int(qty)} 股 (<100), 成本 ${cost_basis:.2f}。\n\n"
            f"时间: {et} (= {sgt})\n\n"
            f"含义: 不足 100 股 → 无法卖备兑认购 (CC); 同时因持有股票, bot 也\n"
            f"不会在此标的卖新认沽 (Put)。该零头仓位目前只受 -35% 止损保护,\n"
            f"不产生权利金收入, 属于\"卡住\"状态。\n\n"
            f"可能原因: 部分行权 / 部分成交 / 手动操作残留。\n\n"
            f"建议二选一:\n"
            f"  1. 补足到 100 股 (再买 {need} 股) → 恢复卖 CC\n"
            f"  2. 清掉这 {int(qty)} 股 → 标的回到 IDLE, 恢复卖 Put\n\n"
            f"(本邮件为只读告警, bot 不会自动补仓或清仓。)"
        )
        ok = send_email(subject=subject, body_text=body)
        if ok:
            state[symbol] = {"last_alert_date": today, "qty": int(qty)}
            _save(state)
            logger.warning(f"📧 partial-holding alert sent: {symbol} x{int(qty)}")
    except Exception as e:
        logger.warning(f"partial_alert 邮件失败: {e}")
