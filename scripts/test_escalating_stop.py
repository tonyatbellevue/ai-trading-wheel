"""Phase 4.1 regression — escalating stock stop-loss.

Verifies the three-state escalation + halt-awareness + oversell guard:
  - Cycle 1-2: LIMIT order @ stock×0.97
  - Cycle 3+ : MARKET order (persistent crash)
  - Halt (non-tradable): no order, no attempt counted
  - Re-pricing cancels prior unfilled stop (no oversell)

Run: python scripts/test_escalating_stop.py   (exit 0 = pass)
"""
from __future__ import annotations
import sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from strategy.wheel_strategy import WheelStrategy
from metrics import stop_state


class _Recorder:
    def __init__(self):
        self.orders = []        # list of submitted order requests
        self.cancels = []

    def submit_order(self, req):
        self.orders.append(req)
        return SimpleNamespace(id="ord-%d" % len(self.orders))

    def cancel_order_by_id(self, oid):
        self.cancels.append(oid)
        # Simulate cancel clearing the order (unless rigged to fail)
        if not self._cancel_fails:
            self._open_orders = [o for o in self._open_orders if o.id != oid]

    def get_orders(self, filter=None):
        return list(self._open_orders)

    def get_asset(self, sym):
        return SimpleNamespace(tradable=self._tradable)

    def get_open_position(self, sym):
        return SimpleNamespace(qty=self._live_qty)


def _run_cycle(symbol, qty, stock_price, cost, tradable=True, open_orders=None,
               cancel_fails=False, live_qty=None):
    """Drive _try_stock_stop_loss once with mocked trading client."""
    w = WheelStrategy.__new__(WheelStrategy)   # bypass __init__ (no Alpaca)
    w.symbol = symbol
    rec = _Recorder()
    rec._tradable = tradable
    rec._open_orders = open_orders or []
    rec._cancel_fails = cancel_fails
    rec._live_qty = qty if live_qty is None else live_qty
    w._trading = rec
    obj = SimpleNamespace(qty=qty, avg_entry_price=cost, current_price=stock_price)
    drop = (stock_price - cost) / cost
    with patch("strategy.wheel_strategy._safe_journal"):
        w._try_stock_stop_loss(obj, stock_price, cost, drop)
    return rec


def _order_type(req):
    cls = type(req).__name__
    return "MARKET" if "Market" in cls else "LIMIT"


def main():
    SYM = "TESTQ"
    fails = []
    print("=" * 64)
    print("Phase 4.1 regression: escalating stock stop-loss")
    print("=" * 64)

    # Clean slate
    stop_state.reset(SYM)

    # Cycle 1: should place LIMIT @ 0.97
    r1 = _run_cycle(SYM, 100, 60.0, 100.0)   # -40% drop
    t1 = _order_type(r1.orders[0]) if r1.orders else "NONE"
    ok1 = (t1 == "LIMIT" and abs(float(r1.orders[0].limit_price) - round(60*0.97,2)) < 0.01)
    print(f"  [{'PASS' if ok1 else 'FAIL'}] Cycle1: {t1} @ "
          f"{getattr(r1.orders[0],'limit_price','?') if r1.orders else '-'} (expect LIMIT @58.20)")
    if not ok1: fails.append("cycle1")

    # Cycle 2: LIMIT again, must cancel prior open order first
    r2 = _run_cycle(SYM, 100, 55.0, 100.0,
                    open_orders=[SimpleNamespace(id="old1", symbol=SYM, side="OrderSide.SELL")])
    t2 = _order_type(r2.orders[0]) if r2.orders else "NONE"
    ok2 = (t2 == "LIMIT" and "old1" in r2.cancels)
    print(f"  [{'PASS' if ok2 else 'FAIL'}] Cycle2: {t2}, cancelled prior={r2.cancels} "
          f"(expect LIMIT + cancel old1)")
    if not ok2: fails.append("cycle2")

    # Cycle 3: should escalate to MARKET
    r3 = _run_cycle(SYM, 100, 50.0, 100.0)
    t3 = _order_type(r3.orders[0]) if r3.orders else "NONE"
    ok3 = (t3 == "MARKET")
    print(f"  [{'PASS' if ok3 else 'FAIL'}] Cycle3: {t3} (expect MARKET escalation)")
    if not ok3: fails.append("cycle3")

    # Halt: non-tradable → no order, attempt count unchanged
    stop_state.reset(SYM)
    before = stop_state.get_attempts(SYM)
    rh = _run_cycle(SYM, 100, 50.0, 100.0, tradable=False)
    after = stop_state.get_attempts(SYM)
    okh = (len(rh.orders) == 0 and after == before)
    print(f"  [{'PASS' if okh else 'FAIL'}] Halt: orders={len(rh.orders)}, "
          f"attempts {before}->{after} (expect 0 orders, no increment)")
    if not okh: fails.append("halt")

    # Oversell guard: prior open order always cancelled before new
    stop_state.reset(SYM)
    ro = _run_cycle(SYM, 100, 60.0, 100.0,
                    open_orders=[SimpleNamespace(id="dup", symbol=SYM, side="OrderSide.SELL")])
    oko = ("dup" in ro.cancels and len(ro.orders) == 1)
    print(f"  [{'PASS' if oko else 'FAIL'}] Oversell guard: cancelled={ro.cancels}, "
          f"new orders={len(ro.orders)} (expect cancel + exactly 1 new)")
    if not oko: fails.append("oversell")

    # Bug-A: cancel FAILS (order still open) → must NOT submit new (no stack)
    stop_state.reset(SYM)
    ra = _run_cycle(SYM, 100, 60.0, 100.0, cancel_fails=True,
                    open_orders=[SimpleNamespace(id="stuck", symbol=SYM, side="OrderSide.SELL")])
    oka = (len(ra.orders) == 0)
    print(f"  [{'PASS' if oka else 'FAIL'}] Cancel-fail: new orders={len(ra.orders)} "
          f"(expect 0 — don't stack when old order stuck)")
    if not oka: fails.append("cancel_fail")

    # Bug-B: limit filled between cycles (live qty=0) → no new order, state reset
    stop_state.reset(SYM)
    rb = _run_cycle(SYM, 100, 50.0, 100.0, live_qty=0)
    okb = (len(rb.orders) == 0 and stop_state.get_attempts(SYM) == 0)
    print(f"  [{'PASS' if okb else 'FAIL'}] Filled-between: orders={len(rb.orders)}, "
          f"attempts={stop_state.get_attempts(SYM)} (expect 0 orders, state reset)")
    if not okb: fails.append("filled_between")

    stop_state.reset(SYM)
    print("=" * 64)
    if fails:
        print(f"FAILURES: {fails}")
        sys.exit(1)
    print("ALL PASS — escalation, halt-awareness, oversell guard all correct.")
    sys.exit(0)


if __name__ == "__main__":
    main()
