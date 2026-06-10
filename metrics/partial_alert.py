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


# Require the partial state to persist this many consecutive cron reads
# before sending an actionable email. A single read can be a transient
# Alpaca paper glitch (real 6/9 incident: one read returned 80 sh @ $442.50
# — the raw strike, a mid-reconciliation snapshot — while the true position
# was 100 sh @ $441.13). Trusting a single read = the exact bug class this
# whole effort fixed for prices; apply the same "confirm across reads" rule.
CONFIRM_READS = 2
# Minimum seconds between counted confirms (cron is 8 min = 480s; this
# rejects the multiple get_phase() calls within a single tick, seconds apart).
MIN_CONFIRM_GAP_S = 240


def maybe_alert(symbol: str, qty: float, cost_basis: float) -> None:
    """Send ONE email per symbol per day about a partial (<100 sh) holding,
    but ONLY after the partial state is confirmed on CONFIRM_READS consecutive
    cron reads (defends against transient bad position reads)."""
    today = date.today().isoformat()
    state = _load()
    entry = state.get(symbol, {})

    if entry.get("last_alert_date") == today:
        return  # already alerted today

    # ── Confirmation gate: count consecutive partial reads across TICKS ──
    # get_phase() runs several times per cron tick, so only count a confirm
    # if the last one was > MIN_GAP_S ago (cron is 8 min; within-tick calls
    # are seconds apart and must NOT each increment).
    import time
    now = time.time()
    last_ts = float(entry.get("last_confirm_ts", 0))
    if now - last_ts < MIN_CONFIRM_GAP_S:
        return  # same cron tick — don't double-count
    confirms = int(entry.get("confirms", 0)) + 1
    entry["confirms"] = confirms
    entry["last_qty"] = int(qty)
    entry["last_confirm_ts"] = now
    state[symbol] = entry
    _save(state)
    if confirms < CONFIRM_READS:
        logger.info(
            f"partial_alert: {symbol} {int(qty)} 股 (<100) 第 {confirms}/{CONFIRM_READS} "
            f"次确认 (跨 tick), 暂不告警 (防瞬时坏读)"
        )
        return

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
