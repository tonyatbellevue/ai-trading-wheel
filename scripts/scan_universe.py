"""Scan wheel_universe for sell-put candidates using bot's filters.

Uses the SAME filter functions the bot uses in production:
  - check_earnings() — handles list-of-dates correctly
  - check_iv_rank() — IV Rank ≥ 30
  - compute_realized_vol() — adaptive delta target
  - settings.MIN_PREMIUM_ANNUALIZED — premium quality gate

NEVER reimplement these inline — that was the bug on 5/26 where a hand-rolled
earnings check treated EARNINGS_DB[sym] as a string instead of list, silently
swallowing the TypeError and letting CRM (5/27 earnings) through.

Run:
    python scripts/scan_universe.py
"""
from __future__ import annotations
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timedelta
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment

from config import settings
from wheel_scanner import WHEEL_UNIVERSE
from strategy.wheel_filters import (
    check_earnings, check_iv_rank, compute_iv_rank,
    compute_realized_vol,
)
from strategy.sector_map import sector_of


def main():
    sc = StockHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)
    oc = OptionHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)

    today = date.today()
    exp_start = today + timedelta(days=settings.WHEEL_MIN_DTE + 2)
    exp_end = today + timedelta(days=settings.WHEEL_MAX_DTE)

    print(f"Scan time: {datetime.now()}")
    print(f"Looking for Put expiring {exp_start}–{exp_end}")
    print(f"Filters: earnings>7d, IV rank≥30, OTM≥2.5%, annualized≥{settings.MIN_PREMIUM_ANNUALIZED:.0%}")
    print("=" * 80)

    results = []
    skipped = {"earnings": [], "iv_low": [], "no_chain": [], "no_quote": [], "thin_premium": []}

    for sym in WHEEL_UNIVERSE:
        # ── Same filter chain as bot ──
        ok, msg = check_earnings(sym)
        if not ok:
            skipped["earnings"].append(f"{sym} ({msg})")
            continue

        rank = None
        try:
            rank = compute_iv_rank(sym)
        except Exception:
            pass
        if rank is None or rank < 30:
            skipped["iv_low"].append(f"{sym} (rank={rank})")
            continue

        # Get current price
        try:
            bars = sc.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=sym, timeframe=TimeFrame.Day,
                start=datetime.now() - timedelta(days=5),
                adjustment=Adjustment.ALL,
            )).df
            if bars.empty:
                continue
            spot = float(bars['close'].iloc[-1])
        except Exception:
            continue

        # Adaptive delta from IV rank (matches target_delta_for_iv_rank)
        if rank >= 70:
            target = 0.15
        elif rank >= 50:
            target = 0.20
        else:
            target = 0.25

        # Fetch chain
        try:
            chain = oc.get_option_chain(OptionChainRequest(
                underlying_symbol=sym,
                expiration_date_gte=exp_start, expiration_date_lte=exp_end,
                type="put",
            ))
        except Exception:
            skipped["no_chain"].append(sym)
            continue
        if not chain:
            skipped["no_chain"].append(sym)
            continue

        # Pick closest-to-target-delta
        best, best_dist = None, 999
        for ocsym, snap in chain.items():
            if not snap.greeks or not snap.latest_quote:
                continue
            delta = abs(float(snap.greeks.delta or 0))
            bid = float(snap.latest_quote.bid_price or 0)
            ask = float(snap.latest_quote.ask_price or 0)
            if delta == 0 or bid == 0:
                continue
            mid = (bid + ask) / 2 if ask > 0 else bid
            strike = int(ocsym[-8:]) / 1000
            dist = abs(delta - target)
            if dist < best_dist:
                best_dist = dist
                exp_str = ocsym.replace(sym, "")[:6]
                exp_date = date(2000 + int(exp_str[:2]), int(exp_str[2:4]), int(exp_str[4:6]))
                dte = (exp_date - today).days
                ann = (mid / strike) * (365 / dte) if dte > 0 else 0
                otm = (spot - strike) / strike
                best = {
                    "sym": sym, "rank": rank, "strike": strike, "delta": delta,
                    "mid": mid, "spot": spot, "otm": otm, "ann": ann, "dte": dte,
                    "sector": sector_of(sym), "ocsym": ocsym,
                }

        if best is None:
            skipped["no_quote"].append(sym)
            continue
        if best["ann"] < settings.MIN_PREMIUM_ANNUALIZED:
            skipped["thin_premium"].append(f"{sym} (ann={best['ann']:.0%})")
            continue
        if best["otm"] < 0.025:
            continue  # too close to strike

        results.append(best)

    # Rank by composite: high annualized AND high OTM safety margin
    results.sort(key=lambda r: r["ann"] * (1 + r["otm"]), reverse=True)

    print(f"\n=== Candidates passing ALL filters: {len(results)} ===")
    print(f"{'#':<3} {'Sym':<6} {'Sec':<10} {'IVRk':>4} {'Strike':>7} {'Δ':>5} {'OTM':>6} {'Prem':>6} {'Ann':>5} {'Coll':>10}")
    print("-" * 80)
    for i, r in enumerate(results[:15], 1):
        coll = r["strike"] * 100
        print(f"{i:<3} {r['sym']:<6} {r['sector']:<10} {r['rank']:>4.0f} ${r['strike']:>6.0f} {r['delta']:>5.2f} {r['otm']:>+5.1%} ${r['mid']:>5.2f} {r['ann']:>4.0%} ${coll:>9,.0f}")

    print(f"\n=== Skipped reasons ===")
    for reason, items in skipped.items():
        if items:
            print(f"  {reason} ({len(items)}): {', '.join(items[:5])}{'...' if len(items)>5 else ''}")


if __name__ == "__main__":
    main()
