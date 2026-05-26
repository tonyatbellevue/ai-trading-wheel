"""Refresh EARNINGS_DB from Finnhub API.

Writes results to data/earnings_cache.json. wheel_filters.py:EARNINGS_DB
checks this JSON file first; only falls back to hardcoded values if JSON
is missing or stale (>7 days old).

Requires FINNHUB_API_KEY env var (free tier 60 calls/min is plenty).
Without the key the script exits 0 with a no-op and the bot keeps using
the hardcoded EARNINGS_DB.

Run manually:
    python scripts/refresh_earnings_db.py

Recommended cron (cron-job.org or GHA): weekly, Monday 06:00 UTC.
"""
from __future__ import annotations
import sys, io, os, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, date, timedelta
from pathlib import Path
import requests

from wheel_scanner import WHEEL_UNIVERSE

CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "earnings_cache.json"


def fetch_finnhub(symbol: str, api_key: str) -> list[str]:
    """Return next ~4 earnings dates as ISO strings, or empty list."""
    try:
        today = date.today()
        end = today + timedelta(days=180)
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={
                "from": today.isoformat(),
                "to": end.isoformat(),
                "symbol": symbol,
                "token": api_key,
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json().get("earningsCalendar", [])
        # Dedupe, sort, take next 4
        seen = set()
        dates = []
        for e in sorted(data, key=lambda x: x.get("date", "")):
            d = e.get("date")
            if d and d not in seen:
                seen.add(d)
                dates.append(d)
        return dates[:4]
    except Exception as e:
        print(f"  ⚠️  {symbol}: {type(e).__name__}: {e}")
        return []


def main():
    api_key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not api_key:
        print("⚠️  FINNHUB_API_KEY not set. Get a free key at finnhub.io/dashboard")
        print("    Then add to .env: FINNHUB_API_KEY=ck_xxx...")
        print("    Skipping refresh — bot will keep using hardcoded EARNINGS_DB.")
        return 0

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    print(f"Refreshing earnings for {len(WHEEL_UNIVERSE)} symbols via Finnhub...")
    print(f"Cache file: {CACHE_FILE}\n")

    new_db = {}
    failed = []
    for sym in WHEEL_UNIVERSE:
        dates = fetch_finnhub(sym, api_key)
        if dates:
            new_db[sym] = dates
            print(f"  ✅ {sym}: {dates}")
        else:
            failed.append(sym)
            print(f"  ⚪ {sym}: no data")

    # Add ETFs as empty (suppresses NO_DATA warning in bot)
    for etf in ("SPY", "QQQ", "DRAM"):
        new_db[etf] = []

    cache_data = {
        "refreshed_at": datetime.now().isoformat(),
        "source": "finnhub",
        "earnings": new_db,
    }
    CACHE_FILE.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")

    print(f"\n✅ Wrote {len(new_db)} symbols to {CACHE_FILE.name}")
    if failed:
        print(f"⚠️  {len(failed)} symbols had no Finnhub data (will fall back to hardcoded):")
        print(f"    {', '.join(failed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
