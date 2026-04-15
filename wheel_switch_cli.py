"""Wheel 标的切换 CLI

用法：
    python wheel_switch_cli.py status                       # 查看当前状态和计划
    python wheel_switch_cli.py plan MSFT 2026-04-18 "换低波动标的"
    python wheel_switch_cli.py cancel                       # 取消计划
    python wheel_switch_cli.py force MSFT                   # 立即强制切换（危险）
"""
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8")

from strategy.wheel_switch import (
    load_state, save_state, plan_switch, cancel_plan,
)


def show_status():
    s = load_state()
    print(f"当前活跃标的: {s['active_symbol']}")
    print()
    plan = s.get("plan")
    if plan:
        print("📅 待执行切换计划:")
        print(f"  {plan['from_symbol']} → {plan['to_symbol']}")
        print(f"  触发日期: ≥{plan['trigger_date']}（today: {date.today()}）")
        print(f"  登记时间: {plan.get('planned_at', '—')}")
        print(f"  原因: {plan.get('reason', '—')}")
        trig = date.fromisoformat(plan['trigger_date'])
        days = (trig - date.today()).days
        if days <= 0:
            print(f"  ✅ 可触发（等当前周期 IDLE 时自动生效）")
        else:
            print(f"  ⏳ 还需 {days} 天")
    else:
        print("（无切换计划）")
    print()
    history = s.get("history", [])
    if history:
        print(f"历史切换 ({len(history)} 次):")
        for h in history[-5:]:
            print(f"  {h['switched_at'][:16]} {h['from']} → {h['to']}: {h.get('reason','—')}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]

    if cmd == "status":
        show_status()

    elif cmd == "plan":
        if len(sys.argv) < 4:
            print("用法: plan TO_SYMBOL YYYY-MM-DD [reason]")
            return
        to_sym = sys.argv[2].upper()
        trigger = sys.argv[3]
        reason = " ".join(sys.argv[4:]) if len(sys.argv) > 4 else ""
        s = load_state()
        from_sym = s["active_symbol"]
        plan_switch(from_sym, to_sym, trigger, reason)
        print()
        show_status()

    elif cmd == "cancel":
        cancel_plan()
        show_status()

    elif cmd == "force":
        if len(sys.argv) < 3:
            print("用法: force TO_SYMBOL")
            return
        to_sym = sys.argv[2].upper()
        s = load_state()
        old = s["active_symbol"]
        s["active_symbol"] = to_sym
        s["plan"] = None
        from datetime import datetime
        s.setdefault("history", []).append({
            "switched_at": datetime.now().isoformat(),
            "from": old, "to": to_sym, "reason": "manual force"
        })
        save_state(s)
        print(f"⚠️  已强制切换 {old} → {to_sym}")
        show_status()

    else:
        print(f"未知命令: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
