"""Morning briefing — 5-in-1 daily snapshot for the wheel bot."""
from __future__ import annotations
import sys, os, io, subprocess, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from loguru import logger
logger.remove()

from config import settings
from core.alpaca_client import AlpacaClients
from strategy.wheel_strategy import _parse_symbol
from strategy.wheel_filters import EARNINGS_DB
from wheel_scanner import WHEEL_UNIVERSE


def section(title): print(f"\n## {title}\n")

def main():
    sgt = datetime.now(ZoneInfo("Asia/Singapore"))
    et = datetime.now(ZoneInfo("America/New_York"))
    print(f"# 🌅 Morning Brief — {sgt:%Y-%m-%d %H:%M SGT}  ({et:%H:%M ET})")

    # ── 1. Account ──
    section("💰 Account")
    try:
        trading = AlpacaClients.trading()
        acct = trading.get_account()
        eq = float(acct.equity)
        cash = float(acct.cash)
        pnl = eq - float(acct.last_equity)
        print(f"| Equity | Cash | BP | Today P&L |")
        print(f"|---|---|---|---|")
        print(f"| ${eq:,.2f} | ${cash:,.2f} | ${float(acct.buying_power):,.2f} | {'+' if pnl>=0 else ''}${pnl:,.2f} |")
    except Exception as e:
        print(f"⚠️ Account lookup failed: {e}")
        return 1

    # ── 2. Positions ──
    section("📊 Positions")
    try:
        positions = trading.get_all_positions()
        opt = [p for p in positions if len(p.symbol) > 10]
        if not opt:
            print("**IDLE** — no open positions")
        else:
            print(f"| Contract | Entry | Current | P&L |")
            print(f"|---|---|---|---|")
            for p in opt:
                pnl = float(p.unrealized_pl)
                sign = "+" if pnl >= 0 else ""
                print(f"| `{p.symbol}` | ${float(p.avg_entry_price):.2f} | ${float(p.current_price):.2f} | {sign}${pnl:.2f} |")
    except Exception as e:
        print(f"⚠️ {e}")

    # ── 3. Bot health (last 24h) ──
    section("🤖 Bot health (last 24h)")
    try:
        r = subprocess.run(["gh", "run", "list", "--workflow=wheel.yml",
                            "--limit=50", "--json", "conclusion,createdAt"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            runs = json.loads(r.stdout)
            cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(hours=24)
            recent = [run for run in runs
                      if datetime.fromisoformat(run["createdAt"].replace("Z", "+00:00")) > cutoff]
            success = sum(1 for run in recent if run["conclusion"] == "success")
            failure = sum(1 for run in recent if run["conclusion"] == "failure")
            total = len(recent)
            if failure == 0:
                print(f"✅ {success}/{total} GHA wheel.yml runs success")
            else:
                print(f"⚠️ {failure}/{total} FAILED in last 24h")
        else:
            print("⚠️ gh CLI not configured or unauth")
    except Exception as e:
        print(f"⚠️ {e}")

    # ── 4. Today's earnings (wheel universe) ──
    section("📅 Today's & tomorrow's earnings (wheel universe)")
    today = et.date()
    tomorrow = today + timedelta(days=1)
    in_window = []
    for sym in WHEEL_UNIVERSE:
        dates = EARNINGS_DB.get(sym, [])
        for d in dates:
            try:
                ed = datetime.fromisoformat(d).date()
                if ed == today:
                    in_window.append((sym, ed, "TODAY ⚠️"))
                elif ed == tomorrow:
                    in_window.append((sym, ed, "tomorrow"))
            except Exception:
                pass
    if not in_window:
        print("✅ No earnings today/tomorrow in wheel universe")
    else:
        for sym, d, when in in_window:
            print(f"- **{sym}** — {when} ({d})")

    # ── 5. Market pre-open ──
    section("🌍 Market context")
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import Adjustment

        sc = StockHistoricalDataClient(api_key=settings.API_KEY, secret_key=settings.SECRET_KEY)
        for sym in ["SPY", "QQQ", "SMH", "XLF", "XLE", "XLV"]:
            try:
                bars = sc.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=sym, timeframe=TimeFrame.Day,
                    start=datetime.now() - timedelta(days=5),
                    adjustment=Adjustment.ALL,
                )).df
                px_today = float(bars['close'].iloc[-1])
                px_prev = float(bars['close'].iloc[-2])
                chg = (px_today - px_prev) / px_prev * 100
                arrow = "🔼" if chg > 0 else ("🔽" if chg < -0.5 else "➡️")
                print(f"- {arrow} **{sym}**: ${px_today:.2f} ({chg:+.2f}%)")
            except Exception:
                pass
    except Exception as e:
        print(f"⚠️ Market data unavailable: {e}")

    # ── 6. Wheel symbol focus ──
    section(f"🎯 Active wheel symbol: {settings.WHEEL_SYMBOL}")
    try:
        from strategy.wheel_filters import compute_iv_rank
        rank = compute_iv_rank(settings.WHEEL_SYMBOL)
        bars = sc.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=settings.WHEEL_SYMBOL, timeframe=TimeFrame.Day,
            start=datetime.now() - timedelta(days=5),
            adjustment=Adjustment.ALL,
        )).df
        px = float(bars['close'].iloc[-1])
        px_3d = float(bars['close'].iloc[-3]) if len(bars) >= 3 else px
        chg_3d = (px - px_3d) / px_3d * 100
        print(f"- Price: ${px:.2f}  (3d {chg_3d:+.1f}%)")
        print(f"- IV Rank: {rank:.0f}" if rank else "- IV Rank: N/A")
    except Exception as e:
        print(f"⚠️ {e}")

    print()
    print("---")
    print(f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
