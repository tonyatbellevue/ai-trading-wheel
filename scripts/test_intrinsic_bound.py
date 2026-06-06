"""Regression test for Phase 3 intrinsic-value bound (4th fair-price layer).

Catches what last/mid layers cannot: a LAST TRADE that is itself a single
anomalous print. Verified 6/5: GM $78P had a 1-lot $0.52 print while the
real value was ~$0.01. If the bot had run during that window, the last-
trade layer would have returned $0.52 (passing the ask×2 clamp). The
Black-Scholes physical bound rejects it.

Run: python scripts/test_intrinsic_bound.py   (exit 0 = pass)
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

from unittest.mock import patch
from strategy.wheel_strategy import WheelStrategy


def _bound(symbol, stock_price, rv, occ):
    w = WheelStrategy(symbol=symbol)
    with patch.object(w, "_get_stock_fair_price", return_value=stock_price), \
         patch("strategy.wheel_filters.compute_realized_vol", return_value=rv):
        return w._theoretical_max_price(occ)


def _finalize(symbol, stock_price, rv, occ, price):
    w = WheelStrategy(symbol=symbol)
    with patch.object(w, "_get_stock_fair_price", return_value=stock_price), \
         patch("strategy.wheel_filters.compute_realized_vol", return_value=rv), \
         patch.object(w, "_record_quote_ok"), patch.object(w, "_record_quote_skip"), \
         patch.object(w, "_record_quote_stat"):
        return w._finalize_fair(occ, price, "test")


def main():
    # (name, symbol, S, rv, occ, price, expect_pass, note)
    cases = [
        ("GM real $0.07", "GM", 82.0, 0.30, "GM260605P00078000", 0.07, True,
         "真实成交残值 → 通过"),
        ("GM real $0.01", "GM", 82.0, 0.30, "GM260605P00078000", 0.01, True,
         "真实归零价 → 通过"),
        ("GM SPIKE last $0.52", "GM", 82.0, 0.30, "GM260605P00078000", 0.52, False,
         "单笔抽风成交 → 拒绝 (last层抓不到, bound抓到)"),
        ("GM mark $2.13", "GM", 82.0, 0.30, "GM260605P00078000", 2.13, False,
         "mark抽风 → 拒绝"),
        ("MSFT CC normal", "MSFT", 431.0, 0.40, "MSFT260608C00442500", 0.70, True,
         "正常 CC 价 → 通过"),
        ("high-IV legit", "AAPL", 100.0, 1.20, "AAPL260612P00095000", 2.18, True,
         "财报高IV真实价 → 通过 (不误杀)"),
        ("ITM put legit", "GM", 70.0, 0.30, "GM260605P00078000", 8.10, True,
         "价内put内在价值$8 → 通过"),
    ]
    fails = []
    print("=" * 64)
    print("Phase 3 intrinsic-value bound: catches poisoned last-trade")
    print("=" * 64)
    for name, sym, S, rv, occ, price, expect_pass, note in cases:
        got = _finalize(sym, S, rv, occ, price)
        passed = got is not None
        ok = (passed == expect_pass)
        status = "PASS" if ok else "FAIL"
        b = _bound(sym, S, rv, occ)
        print(f"  [{status}] {name}: price=${price} bound=${b:.2f} → "
              f"{'通过' if passed else '拒绝'} ({note})")
        if not ok:
            fails.append(name)

    print("=" * 64)
    if fails:
        print(f"FAILURES: {fails}")
        sys.exit(1)
    print("ALL PASS — poisoned last-trade caught, legit prices (incl high-IV) pass.")
    sys.exit(0)


if __name__ == "__main__":
    main()
