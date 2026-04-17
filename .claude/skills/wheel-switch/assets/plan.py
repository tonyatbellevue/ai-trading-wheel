"""Plan a wheel symbol rotation."""
from __future__ import annotations
import sys, os, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from loguru import logger
logger.remove()

from strategy.wheel_switch import load_state, plan_switch
from strategy.wheel_evaluator import _next_expiry_friday_plus_one
from wheel_scanner import WHEEL_UNIVERSE


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: plan.py <TO_SYMBOL> [reason...]")
        return 2

    to_sym = argv[1].upper()
    reason = " ".join(argv[2:]) if len(argv) > 2 else "manual rotation via wheel-switch skill"

    if to_sym not in WHEEL_UNIVERSE:
        print(f"❌ {to_sym} is not in WHEEL_UNIVERSE ({len(WHEEL_UNIVERSE)} symbols).")
        print("   Accepted: " + ", ".join(sorted(WHEEL_UNIVERSE)))
        print("   If you want to expand the pool, edit wheel_scanner.py first.")
        return 2

    state = load_state()
    current = state.get("active_symbol", "TSLA")
    if to_sym == current:
        print(f"ℹ️  Already on {current}. Nothing to do.")
        return 0

    trigger = _next_expiry_friday_plus_one()
    plan_switch(current, to_sym, trigger.isoformat(), reason)

    # Confirm what was written
    state_after = load_state()
    plan = state_after.get("plan", {})

    print(f"✅ Rotation planned")
    print(f"   From:    {current}")
    print(f"   To:      {to_sym}")
    print(f"   Trigger: ≥ {trigger.isoformat()}")
    print(f"   Reason:  {reason}")
    print()
    print(f"📋 wheel_symbol.json.plan = {plan}")
    print()
    print("This is a *plan*, not an immediate swap. The bot will switch on the "
          "next run_cycle after the trigger date IF it is IDLE (no open puts/calls/stock).")
    print()
    print("• Cancel:    python wheel_switch_cli.py cancel")
    print("• Force now: python wheel_switch_cli.py force " + to_sym +
          "   (dangerous — skips IDLE check)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
