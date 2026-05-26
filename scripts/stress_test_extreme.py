"""Stress test: replay historical extreme scenarios against new bot features.

Tests three crash periods that would exercise the new safety layers:
  1. 2020-03 COVID crash (TSLA, AVGO, AAPL -30% in 3 weeks)
  2. 2022-06 bear market (TSLA, NVDA -25% over 1 month)
  3. 2024-08 yen carry unwind (NVDA, AVGO -20% in 5 days)

For each scenario simulates:
  - Drop protection: when -20% pause triggers, when -35% stop-loss fires
  - v7 OTM% framework: would it correctly skip BTC vs force BTC?
  - Min premium gate (20% annualized): would low-IV days be blocked?

Run:
    python scripts/stress_test_extreme.py
"""
from __future__ import annotations
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, date, timedelta
from math import log, sqrt
from statistics import stdev

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment
from config import settings

client = StockHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)


SCENARIOS = [
    ("2020 COVID Crash",  date(2020, 2, 15), date(2020, 4, 15), ["AAPL", "AVGO", "AMD"]),
    ("2022 Bear Market",  date(2022, 5, 15), date(2022, 7, 15), ["TSLA", "NVDA", "AVGO"]),
    ("2024 Yen Unwind",   date(2024, 7, 25), date(2024, 8, 15), ["NVDA", "AVGO", "TSLA"]),
    ("2025 Tariff Panic", date(2025, 4, 1),  date(2025, 4, 30), ["AVGO", "TSLA", "NVDA"]),
]


def fetch_bars(sym, start, end):
    """Daily closes for [start, end]."""
    req = StockBarsRequest(
        symbol_or_symbols=sym, timeframe=TimeFrame.Day,
        start=datetime(start.year, start.month, start.day),
        end=datetime(end.year, end.month, end.day),
        adjustment=Adjustment.ALL,
    )
    bars = client.get_stock_bars(req).df
    if bars.empty:
        return []
    return [(idx[1].date(), float(row['close']))
            for idx, row in bars.iterrows()]


def annualized_rv(closes_window):
    """Annualized realized vol from a list of closes."""
    if len(closes_window) < 5:
        return None
    rets = [log(closes_window[i]/closes_window[i-1])
            for i in range(1, len(closes_window))]
    return stdev(rets) * sqrt(252)


def simulate_drop_protection(name, bars, sym):
    """Pretend we got assigned at the first day's close, then watch drop."""
    if len(bars) < 5:
        return None
    assign_date, cost = bars[0]
    print(f"  → 假设 {assign_date} 被行权拿到 {sym} @ ${cost:.2f}")

    triggered_20 = None
    triggered_35 = None
    max_drop = 0.0
    for d, px in bars[1:]:
        drop = (px - cost) / cost
        if drop < max_drop:
            max_drop = drop
        if drop <= -0.35 and triggered_35 is None:
            triggered_35 = (d, px, drop)
        elif drop <= -0.20 and triggered_20 is None:
            triggered_20 = (d, px, drop)

    if triggered_35:
        d, px, dp = triggered_35
        print(f"  🔴 -35% 止损触发: {d} 股价 ${px:.2f} 跌幅 {dp:.1%} → 市价卖出股票")
    elif triggered_20:
        d, px, dp = triggered_20
        print(f"  ⏸️ -20% 暂停 CC: {d} 股价 ${px:.2f} 跌幅 {dp:.1%} → 等反弹")
    else:
        print(f"  ✓ 跌幅保护未触发（最大跌幅 {max_drop:.1%}）")
    return max_drop


def simulate_v7_otm(name, bars, sym):
    """For each 5-day window, pretend we sold a Put at delta~0.25 (~3% OTM)
    on day 0, then check what v7 logic would do on the last day."""
    triggered_force_btc = 0
    triggered_safe = 0
    triggered_grey = 0
    total = 0
    for i in range(0, len(bars) - 5, 3):
        sell_date, sell_px = bars[i]
        strike = round(sell_px * 0.97, 0)  # 3% OTM Put
        # last day (DTE=1)
        last_date, last_px = bars[i + 4]
        otm_pct = (last_px - strike) / strike

        # compute realized vol over preceding 20 days
        prev_window = [c for _, c in bars[max(0, i-20):i]]
        rv = annualized_rv(prev_window) if len(prev_window) >= 5 else None
        safe_otm = 0.05 if (rv and rv >= 0.60) else 0.03

        total += 1
        if otm_pct >= safe_otm:
            triggered_safe += 1
        elif otm_pct >= 0.01:
            triggered_grey += 1
        else:
            triggered_force_btc += 1

    if total == 0:
        return
    print(f"  v7 OTM% 判断（{total} 个 5天滚动窗口）：")
    print(f"    免BTC (OTM≥safe): {triggered_safe} ({triggered_safe/total:.0%})")
    print(f"    灰色地带:         {triggered_grey} ({triggered_grey/total:.0%})")
    print(f"    强制BTC (OTM<1%): {triggered_force_btc} ({triggered_force_btc/total:.0%})")


def simulate_min_premium(name, bars, sym):
    """For each day, estimate IV from realized vol, derive expected premium
    for 3% OTM Put @ DTE=4, check if it passes the 20% annualized gate."""
    blocked = 0
    passed = 0
    for i in range(20, len(bars) - 5):
        window = [c for _, c in bars[i-20:i]]
        rv = annualized_rv(window)
        if rv is None:
            continue
        _, px = bars[i]
        strike = round(px * 0.97, 0)
        # Premium approx via Black-Scholes intuition: for 4 DTE,
        # OTM Put premium ≈ strike × rv × sqrt(dte/365) × N(d)
        # rough: premium ≈ strike × rv × 0.02 × 0.3
        dte = 4
        # very rough approximation: premium ≈ 0.2% × strike × (rv / 0.30)
        prem_estimate = strike * 0.002 * (rv / 0.30)
        annualized = (prem_estimate / strike) * (365 / dte)
        if annualized < 0.20:
            blocked += 1
        else:
            passed += 1
    total = blocked + passed
    if total == 0:
        return
    print(f"  最低权利金门槛（{total} 个交易日）：")
    print(f"    通过: {passed} ({passed/total:.0%}) | 拦截: {blocked} ({blocked/total:.0%})")


def main():
    print("=" * 70)
    print("STRESS TEST: 历史极端场景回放（验证今日新功能）")
    print("=" * 70)
    for name, start, end, syms in SCENARIOS:
        print(f"\n## {name} ({start} → {end})")
        for sym in syms:
            print(f"\n[{sym}]")
            bars = fetch_bars(sym, start, end)
            if not bars:
                print(f"  ⚠️ 无数据")
                continue
            first_px = bars[0][1]
            last_px = bars[-1][1]
            total_chg = (last_px - first_px) / first_px
            print(f"  期间走势: ${first_px:.2f} → ${last_px:.2f} ({total_chg:+.1%})")
            simulate_drop_protection(name, bars, sym)
            simulate_v7_otm(name, bars, sym)
            simulate_min_premium(name, bars, sym)
    print("\n" + "=" * 70)
    print("✅ 压力测试完成")


if __name__ == "__main__":
    main()
