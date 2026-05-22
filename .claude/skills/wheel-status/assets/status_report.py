"""Wheel bot status snapshot — read-only."""
from __future__ import annotations
import sys, os, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# Make project root importable when invoked from anywhere
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from datetime import date, datetime
from loguru import logger
logger.remove()   # suppress noisy INFO during one-shot print

from config import settings
from core.alpaca_client import AlpacaClients
from strategy.wheel_strategy import _parse_symbol


def main() -> int:
    trading = AlpacaClients.trading()
    acct = trading.get_account()
    eq = float(acct.equity)
    cash = float(acct.cash)
    bp = float(acct.buying_power)
    le = float(acct.last_equity)
    day_pnl = eq - le
    day_pct = (day_pnl / le * 100) if le > 0 else 0

    try:
        positions = trading.get_all_positions()
    except Exception as e:
        print(f"❌ get_all_positions failed: {e}")
        print("If this is 'unauthorized' the Alpaca key rotated. "
              "Update .env + GitHub Secrets (gh secret set ALPACA_API_KEY).")
        return 1

    short_puts = []
    long_stock = []
    total_collateral = 0.0
    for p in positions:
        info = _parse_symbol(p.symbol)
        qty = float(p.qty)
        if info and info["type"] == "P" and qty < 0:
            coll = info["strike"] * 100 * abs(qty)
            total_collateral += coll
            short_puts.append((p, info, coll))
        elif info and info["type"] == "C" and qty < 0:
            short_puts.append((p, info, 0))   # covered call, no new cash collateral
        elif not info and qty > 0:
            long_stock.append(p)

    now_sgt = datetime.now().strftime("%Y-%m-%d %H:%M SGT")
    max_total_cap = eq * settings.MAX_TOTAL_EXPOSURE_PCT
    buffer_ceiling = eq * settings.CASH_BUFFER_PCT
    exposure_pct = total_collateral / eq * 100 if eq > 0 else 0
    exposure_headroom = max_total_cap - total_collateral
    exposure_warn = "⚠️" if exposure_pct > 80 else "✓"

    # ── Account block ─────────────────────────────────────────────
    print(f"## 📊 Wheel Status — {now_sgt}\n")
    print("| Metric | Value |")
    print("|--------|-------|")
    print(f"| Equity | ${eq:,.2f} |")
    print(f"| Cash | ${cash:,.2f} |")
    print(f"| Buying Power | ${bp:,.2f} |")
    sign = "+" if day_pnl >= 0 else ""
    print(f"| Today P&L | {sign}${day_pnl:,.2f} ({sign}{day_pct:.2f}%) |")
    print(f"| Total put collateral | ${total_collateral:,.2f} ({exposure_pct:.1f}% of equity) {exposure_warn} |")
    print(f"| Remaining headroom | ${exposure_headroom:,.2f} before {settings.MAX_TOTAL_EXPOSURE_PCT:.0%} cap |")
    print(f"| Cash buffer reserve | ${buffer_ceiling:,.2f} ({settings.CASH_BUFFER_PCT:.0%} of equity) |")
    print()

    # ── Positions ─────────────────────────────────────────────────
    if not short_puts and not long_stock:
        print("**Positions:** none (IDLE)\n")
        return 0

    if short_puts:
        print("### Option positions")
        print("| Contract | Strike | Expiry | DTE | Qty | Entry | Current | Unrealized P&L | Cushion |")
        print("|---|---|---|---|---|---|---|---|---|")
        for p, info, _ in short_puts:
            dte = (info["expiry"] - date.today()).days
            entry = float(p.avg_entry_price)
            cur = float(p.current_price)
            upl = float(p.unrealized_pl)
            sign = "+" if upl >= 0 else ""
            # Pull spot for cushion calc.
            # After-hours quotes can have ask=$0 with a stale bid; using that
            # bid alone falsely flags positions as ITM. Validate spread first,
            # fall back to most recent daily close if quote is unreliable.
            try:
                from alpaca.data.historical import StockHistoricalDataClient
                from alpaca.data.requests import (
                    StockLatestQuoteRequest, StockBarsRequest
                )
                from alpaca.data.timeframe import TimeFrame
                from datetime import timedelta
                stk = StockHistoricalDataClient(api_key=settings.API_KEY,
                                                 secret_key=settings.SECRET_KEY)
                q = stk.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=info["underlying"]))[info["underlying"]]
                bid = float(q.bid_price or 0)
                ask = float(q.ask_price or 0)
                # Valid two-sided quote: both > 0 AND spread < 5%
                spot = None
                if bid > 0 and ask > 0 and ask >= bid:
                    spread_pct = (ask - bid) / bid
                    if spread_pct < 0.05:
                        spot = (bid + ask) / 2
                # Fallback: most recent daily close (works pre/post market + weekends)
                if spot is None:
                    bars = stk.get_stock_bars(StockBarsRequest(
                        symbol_or_symbols=info["underlying"],
                        timeframe=TimeFrame.Day,
                        start=datetime.now() - timedelta(days=10),
                    )).df
                    if not bars.empty:
                        spot = float(bars['close'].iloc[-1])
            except Exception:
                spot = None

            if info["type"] == "P" and spot:
                cushion = spot - info["strike"]
                cushion_txt = f"${cushion:+.2f} ({'OTM ✓' if cushion > 0 else 'ITM ⚠️'})"
            elif info["type"] == "C" and spot:
                cushion = info["strike"] - spot
                cushion_txt = f"${cushion:+.2f} ({'OTM ✓' if cushion > 0 else 'ITM ⚠️'})"
            else:
                cushion_txt = "—"
            typ = "PUT" if info["type"] == "P" else "CALL"
            print(f"| `{p.symbol}` | ${info['strike']:.2f} {typ} | "
                  f"{info['expiry']} | {dte}d | {int(float(p.qty))} | "
                  f"${entry:.2f} | ${cur:.2f} | {sign}${upl:,.2f} | {cushion_txt} |")
        print()

    if long_stock:
        print("### Stock positions")
        for p in long_stock:
            print(f"- `{p.symbol}` {p.qty} shares @ ${float(p.avg_entry_price):.2f} "
                  f"| P&L ${float(p.unrealized_pl):+,.2f}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
