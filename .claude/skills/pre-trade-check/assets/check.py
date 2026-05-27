"""Pre-trade safety scan — 8-point check before manual real-money option trade.

Usage:
    python .claude/skills/pre-trade-check/assets/check.py SYMBOL [STRIKE] [EXPIRY]

Example:
    python .claude/skills/pre-trade-check/assets/check.py CRM 165 2026-05-29
    python .claude/skills/pre-trade-check/assets/check.py AVGO         # just check the symbol's safety
"""
from __future__ import annotations
import sys, os, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from datetime import datetime, date, timedelta
from loguru import logger
logger.remove()  # quiet INFO during one-shot

from config import settings
from strategy.wheel_filters import (
    check_earnings, check_iv_rank, check_vix_regime, check_ma_trend,
    check_realized_vol, check_recent_drop, check_sector_etf,
    compute_iv_rank,
)
from strategy.sector_map import sector_of


def fmt(ok, msg):
    return ("🟢" if ok else "🔴"), msg


def main():
    if len(sys.argv) < 2:
        print("Usage: check.py SYMBOL [STRIKE] [EXPIRY]")
        return 1

    sym = sys.argv[1].upper()
    strike = float(sys.argv[2]) if len(sys.argv) > 2 else None
    expiry = sys.argv[3] if len(sys.argv) > 3 else None

    print(f"# Pre-Trade Check: {sym}" + (f" ${strike}P" if strike else "") + (f" exp {expiry}" if expiry else ""))
    print()

    # --- 8 bot filters ---
    checks = [
        ("Earnings (7d window)", check_earnings(sym)),
        ("IV rank ≥ 30",         check_iv_rank(sym)),
        ("VIX regime",            check_vix_regime()),
        ("MA50 trend",            check_ma_trend(sym)),
        ("Realized vol cap",      check_realized_vol(sym)),
        ("Recent 3d drop",        check_recent_drop(sym)),
        ("Sector ETF today",      check_sector_etf(sym)),
    ]

    passes, warnings, blockers = [], [], []
    for name, (ok, msg) in checks:
        icon, _ = fmt(ok, msg)
        line = f"- {icon} **{name}**: {msg}"
        (passes if ok else blockers).append(line)

    # --- Real-money extras ---
    print("## Bot filter results")
    print()
    if passes:
        print("### ✅ Passing")
        for p in passes:
            print(p)
        print()
    if blockers:
        print("### 🔴 Blocking")
        for b in blockers:
            print(b)
        print()

    # --- OTM% + annualized (if strike + expiry given) ---
    if strike and expiry:
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
            from alpaca.data.enums import Adjustment

            sc = StockHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)
            bars = sc.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=sym, timeframe=TimeFrame.Day,
                start=datetime.now() - timedelta(days=5),
                adjustment=Adjustment.ALL,
            )).df
            spot = float(bars['close'].iloc[-1])
            otm_pct = (spot - strike) / strike

            exp_d = datetime.strptime(expiry, "%Y-%m-%d").date()
            dte = (exp_d - date.today()).days

            print("## Real-money extras")
            print(f"- 📍 **Spot price**: ${spot:.2f}")
            print(f"- 📏 **OTM%**: {otm_pct:+.1%} ({'safe' if otm_pct >= 0.03 else 'TIGHT'})")
            print(f"- 📅 **DTE**: {dte} days")
            sec = sector_of(sym)
            print(f"- 🏷️  **Sector**: {sec}")
            print()
        except Exception as e:
            print(f"⚠️ Could not fetch spot/strike data: {e}\n")

    # --- Recommendation ---
    print("## 🎯 Recommendation")
    if blockers:
        print(f"\n**🔴 STOP** — {len(blockers)} filter(s) failed. Skip this trade.")
        for b in blockers:
            print(b.replace("- 🔴", "  •"))
    elif warnings:
        print(f"\n**🟡 CAUTION** — proceed with reduced size.")
    else:
        print("\n**🟢 GO** — all filters passing.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
