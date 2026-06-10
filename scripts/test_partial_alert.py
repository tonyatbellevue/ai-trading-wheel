"""Regression test for Phase 4.4 partial-holding alert confirmation gate.

Real 6/9 incident: a single transient Alpaca read returned MSFT 80 sh @
$442.50 (the raw strike — a mid-reconciliation glitch) while the true
position was 100 sh @ $441.13. The original alert sent an actionable email
on that ONE bad read. Fix: require the partial state to persist across
CONFIRM_READS consecutive cron TICKS (time-gated so multiple get_phase()
calls within one tick don't each count).

Run: python scripts/test_partial_alert.py   (exit 0 = pass)
"""
from __future__ import annotations
import sys, os, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from unittest.mock import patch
from metrics import partial_alert as pa

SYM = "TESTPA"


def _reset_file():
    if pa.STATE_FILE.exists():
        os.remove(pa.STATE_FILE)


def _count_emails(fn):
    sent = []
    with patch("utils.emailer.send_email",
               side_effect=lambda **k: sent.append(k["subject"]) or True):
        fn()
    return sent


def main():
    fails = []
    print("=" * 64)
    print("Phase 4.4 partial-alert confirmation gate")
    print("=" * 64)

    # 1. Single read (one tick) → NO email (only 1 confirm < 2)
    _reset_file()
    s1 = _count_emails(lambda: pa.maybe_alert(SYM, 80, 442.50))
    ok1 = (len(s1) == 0)
    print(f"  [{'PASS' if ok1 else 'FAIL'}] 单次读数 → 邮件 {len(s1)} 封 (应 0, 仅1次确认)")
    if not ok1: fails.append("single-read")

    # 2. Multiple calls SAME tick (seconds apart) → still NO email,
    #    confirms must NOT advance past 1 (time gate)
    _reset_file()
    def same_tick():
        pa.maybe_alert(SYM, 80, 442.50)
        pa.maybe_alert(SYM, 80, 442.50)
        pa.maybe_alert(SYM, 80, 442.50)
    s2 = _count_emails(same_tick)
    confirms2 = pa._load().get(SYM, {}).get("confirms", 0)
    ok2 = (len(s2) == 0 and confirms2 == 1)
    print(f"  [{'PASS' if ok2 else 'FAIL'}] 同tick 3次调用 → 邮件 {len(s2)} 封, confirms={confirms2} "
          f"(应 0封, confirms=1 — 时间门拦住)")
    if not ok2: fails.append("same-tick")

    # 3. Transient bad read then recovery → reset clears, no email
    _reset_file()
    pa.maybe_alert(SYM, 80, 442.50)        # tick 1: bad read, confirm=1
    pa.reset(SYM)                          # tick 2: recovered to 100 → reset
    after_reset = SYM not in pa._load()
    ok3 = after_reset
    print(f"  [{'PASS' if ok3 else 'FAIL'}] 坏读后恢复100 → reset 清除 = {after_reset} (应 True)")
    if not ok3: fails.append("recovery-reset")

    # 4. Persistent partial across 2 ticks → email sent
    _reset_file()
    s4 = []
    with patch("utils.emailer.send_email",
               side_effect=lambda **k: s4.append(k["subject"]) or True):
        pa.maybe_alert(SYM, 80, 442.50)    # tick 1: confirm=1, no email
        # simulate next tick: backdate last_confirm_ts past the gap
        st = pa._load()
        st[SYM]["last_confirm_ts"] = time.time() - (pa.MIN_CONFIRM_GAP_S + 10)
        pa._save(st)
        pa.maybe_alert(SYM, 80, 442.50)    # tick 2: confirm=2 → email
    ok4 = (len(s4) == 1)
    print(f"  [{'PASS' if ok4 else 'FAIL'}] 持续2 tick零头 → 邮件 {len(s4)} 封 (应 1)")
    if not ok4: fails.append("persistent")

    # 5. Daily throttle: after sending, same day no second email
    s5 = _count_emails(lambda: pa.maybe_alert(SYM, 80, 442.50))
    ok5 = (len(s5) == 0)
    print(f"  [{'PASS' if ok5 else 'FAIL'}] 已发后同日再调 → 邮件 {len(s5)} 封 (应 0, 日节流)")
    if not ok5: fails.append("throttle")

    _reset_file()
    print("=" * 64)
    if fails:
        print(f"FAILURES: {fails}")
        sys.exit(1)
    print("ALL PASS — single/transient read no longer sends false alert.")
    sys.exit(0)


if __name__ == "__main__":
    main()
