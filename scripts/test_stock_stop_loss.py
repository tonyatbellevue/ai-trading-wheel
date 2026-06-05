"""Regression test for Bug #2 — -35% stock stop-loss data validation.

The -35% stock stop previously used pos.current_price (mark) alone and
MARKET-sold the whole position. A single bad low print could dump 100
shares at a crazy price (worse than the GM option incident).

Fix: _get_stock_fair_price() cross-checks a fresh bid/ask mid against the
mark; if data is unreliable (one-sided / >10% disagreement) it returns
None and the stop is skipped this cycle. The sell is now a LIMIT order
(mid × 0.98), not market.

Run: python scripts/test_stock_stop_loss.py   (exit 0 = pass)
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


class _FakeQuote:
    def __init__(self, bid, ask):
        self.bid_price = bid
        self.ask_price = ask


class _FakeTrade:
    def __init__(self, price):
        self.price = price


def _fair(symbol, bid, ask, last, mark):
    w = WheelStrategy(symbol=symbol)
    with patch("alpaca.data.historical.StockHistoricalDataClient.get_stock_latest_quote",
               return_value={symbol: _FakeQuote(bid, ask)}), \
         patch("alpaca.data.historical.StockHistoricalDataClient.get_stock_latest_trade",
               return_value={symbol: _FakeTrade(last)}):
        return w._get_stock_fair_price(mark)


def main():
    # Design: anchor on LATEST TRADE, not lagging mark.
    # case: bid, ask, last, mark, expect_none, note
    cases = [
        ("healthy: mid≈last≈mark", 82.0, 82.1, 82.0, 82.0, False,
         "mid 82.05, last 82 → trust"),
        ("REAL CRASH (mark lags!)", 60.0, 60.1, 60.0, 92.0, False,
         "mid 60 + last 60 agree, mark stale 92 → STOP MUST FIRE (concern D)"),
        ("tight BAD quote vs last", 60.0, 60.1, 82.0, 82.0, True,
         "mid 60 but last trade 82 (27% off) → suspect bad quote → None"),
        ("wide spread (bad print)", 70.0, 82.0, 82.0, 82.0, True,
         "spread 16% >5% → unreliable → None"),
        ("one-sided bid=0", 0.0, 82.1, 82.0, 82.0, True,
         "bid=0 → None"),
        ("one-sided ask=0", 82.0, 0.0, 82.0, 82.0, True,
         "ask=0 → None"),
        ("crash, mid&last both low", 53.0, 53.1, 53.0, 82.0, False,
         "real -35% crash: mid+last agree at 53, mark lags 82 → fire"),
    ]
    fails = []
    print("=" * 64)
    print("Bug #2 regression: -35% stock stop-loss data validation")
    print("(anchors on last-trade, NOT lagging mark — concern D fix)")
    print("=" * 64)
    for name, bid, ask, last, mark, expect_none, note in cases:
        got = _fair("GM", bid, ask, last, mark)
        is_none = got is None
        ok = (is_none == expect_none)
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}: fair={got} ({note})")
        if not ok:
            fails.append(name)

    print("=" * 64)
    if fails:
        print(f"FAILURES: {fails}")
        sys.exit(1)
    print("ALL PASS — bad stock data can't trigger a false -35% market dump.")
    sys.exit(0)


if __name__ == "__main__":
    main()
