"""Regression test: OTM% direction must respect contract type (Call vs Put).

Bug (found 8/5): both v11 "let winners ride" and the v7 expiry-day logic
computed `otm_pct = (stock - strike) / strike`, which is PUT-oriented. A
covered call that is SAFELY out of the money (stock BELOW strike) came out
negative, so:
  - v11 (needs OTM ≥ 5%) could never fire for a call
  - v7 on expiry day saw "< 1% → 太接近行权价" and force-bought-back a call
    that was about to expire worthless for free (~$5-15 wasted per cycle)

Real case: MSFT 100sh + 8/7 $505 CC with MSFT at $490.62. True cushion is
+2.85% OTM; the old formula reported -2.85% (looked ITM / at risk).

Run: python scripts/test_otm_direction.py   (exit 0 = pass)
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

from strategy.wheel_strategy import _otm_pct


def main():
    # (name, stock, strike, type, expected_sign_positive, approx_abs_pct)
    cases = [
        ("PUT  OTM  (stock $82 > strike $78)",   82.0,  78.0,  "P", True,  5.13),
        ("PUT  ITM  (stock $74 < strike $78)",   74.0,  78.0,  "P", False, 5.13),
        ("PUT  ATM  (stock == strike)",          78.0,  78.0,  "P", None,  0.0),
        ("CALL OTM  (stock $490.62 < $505) ★",  490.62, 505.0, "C", True,  2.85),
        ("CALL ITM  (stock $520 > strike $505)", 520.0, 505.0, "C", False, 2.97),
        ("CALL ATM  (stock == strike)",         505.0, 505.0, "C", None,  0.0),
        ("CALL deep OTM (stock $450 < $505)",   450.0, 505.0, "C", True,  10.89),
        ("PUT  deep OTM (stock $100 > $78)",    100.0,  78.0,  "P", True,  28.21),
    ]
    fails = []
    print("=" * 68)
    print("OTM direction by contract type (Call vs Put)")
    print("=" * 68)
    for name, s, k, typ, pos_expected, approx in cases:
        v = _otm_pct(s, k, typ)
        if pos_expected is None:
            ok = abs(v) < 1e-9
        else:
            ok = ((v > 0) == pos_expected) and abs(abs(v) * 100 - approx) < 0.05
        label = "价外 OTM" if v > 0 else ("价内 ITM" if v < 0 else "平价 ATM")
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {v:+.2%} ({label})")
        if not ok:
            fails.append(name)

    # Guard: strike <= 0 must not divide by zero
    ok0 = (_otm_pct(100.0, 0.0, "C") == 0.0 and _otm_pct(100.0, -5.0, "P") == 0.0)
    print(f"  [{'PASS' if ok0 else 'FAIL'}] strike ≤ 0 → 0.0 (无除零崩溃)")
    if not ok0:
        fails.append("zero-strike")

    # The exact production case that exposed the bug
    old = (490.62 - 505.0) / 505.0          # legacy put-oriented formula
    new = _otm_pct(490.62, 505.0, "C")
    ok_case = (old < 0 < new)
    print(f"  [{'PASS' if ok_case else 'FAIL'}] 生产案例 MSFT $505C @ $490.62: "
          f"旧={old:+.2%}(误判ITM) → 新={new:+.2%}(正确OTM)")
    if not ok_case:
        fails.append("prod-case")

    print("=" * 68)
    if fails:
        print(f"FAILURES: {fails}")
        sys.exit(1)
    print("ALL PASS — Call/Put 价内外方向都正确, 认购不再被误判。")
    sys.exit(0)


if __name__ == "__main__":
    main()
