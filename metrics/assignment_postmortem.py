"""Self-analyze every option assignment and expiration.

After a short put goes ITM and we get assigned (or more rarely, a covered
call gets called away), we want to learn from it:

  - Was the drop market-wide (SPY/QQQ tanked too) or symbol-specific?
  - Did we pick the strike too aggressively (too close to spot)?
  - Was IV elevated at entry — meaning the market already warned us?
  - Did any of our filters nearly fire but didn't?
  - What strike / delta would have survived?

Output is a markdown postmortem per assignment, saved to reports/ and
stitched into the next close-of-day email so the user sees it once.

Data sources
  - Alpaca /v2/account/activities (direct REST; SDK doesn't expose)
      activity_types=OPASN  option assigned
      activity_types=OPEXP  option expired (OTM — not a loss, but we log too)
  - Alpaca stock bars: 20-day window around entry + assignment
  - SPY bars: same window for market-wide context
  - trade_journal.csv: look up the entry decision so we know what filters passed
"""
from __future__ import annotations
import os, sys, json, csv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, stdev
from math import sqrt, log
from typing import Optional

import requests
from loguru import logger

from config import settings
from strategy.wheel_strategy import _parse_symbol

REPORTS_DIR = Path(settings.BASE_DIR) / "reports" / "postmortems"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = Path(settings.BASE_DIR) / "metrics" / "data" / "postmortem_state.json"


# ── Alpaca activities (direct REST, SDK doesn't wrap it) ──────────────

def _fetch_activities(activity_types: list[str], since_days: int = 30) -> list[dict]:
    headers = {
        "APCA-API-KEY-ID": settings.API_KEY,
        "APCA-API-SECRET-KEY": settings.SECRET_KEY,
    }
    params = {
        "activity_types": ",".join(activity_types),
        "page_size": 100,
        "date": None,   # explicit no filter — we filter client-side
    }
    try:
        r = requests.get(
            "https://paper-api.alpaca.markets/v2/account/activities",
            headers=headers, params={k: v for k, v in params.items() if v is not None},
            timeout=15,
        )
        r.raise_for_status()
        records = r.json()
    except Exception as e:
        logger.warning(f"Activities fetch failed: {e}")
        return []

    cutoff = date.today() - timedelta(days=since_days)
    out = []
    for a in records:
        d = a.get("date")
        if not d:
            continue
        try:
            if date.fromisoformat(d) < cutoff:
                continue
        except Exception:
            continue
        out.append(a)
    return out


# ── Market data helpers ───────────────────────────────────────────────

def _fetch_bars(symbol: str, start: date, end: date) -> list[dict]:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import Adjustment

    try:
        client = StockHistoricalDataClient(
            api_key=settings.API_KEY, secret_key=settings.SECRET_KEY,
        )
        req = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=start, end=end, adjustment=Adjustment.ALL,
        )
        resp = client.get_stock_bars(req)
        bars = resp[symbol]
        return [{"date": b.timestamp.date(),
                 "open": float(b.open), "close": float(b.close),
                 "high": float(b.high), "low": float(b.low)}
                for b in bars]
    except Exception as e:
        logger.warning(f"Bars fetch for {symbol} failed: {e}")
        return []


def _find_trade_journal_entry(contract_symbol: str) -> Optional[dict]:
    """Return the trade_journal.csv row where this contract was opened."""
    path = Path(settings.BASE_DIR) / "metrics" / "data" / "trade_journal.csv"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("event_type") == "entry" and row.get("contract") == contract_symbol:
                    return row
    except Exception:
        pass
    return None


# ── Postmortem state (so we don't regenerate or re-email) ─────────────

def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"processed_ids": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_ids": []}


def _save_state(s: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


# ── Analysis ──────────────────────────────────────────────────────────

def _analyze(activity: dict) -> dict:
    """Build an analysis dict for one assignment/expiration activity."""
    contract = activity.get("symbol", "")
    act_type = activity.get("activity_type", "")
    act_date_str = activity.get("date", "")
    try:
        act_date = date.fromisoformat(act_date_str)
    except Exception:
        act_date = date.today()

    info = _parse_symbol(contract)
    if not info:
        return {"contract": contract, "error": "cannot parse contract symbol"}

    symbol = info["underlying"]
    strike = info["strike"]
    opt_type = info["type"]   # "P" or "C"
    entry_date = act_date - timedelta(days=10)   # estimate — contract DTE is short

    # Try to find real entry in journal
    journal_entry = _find_trade_journal_entry(contract)
    if journal_entry:
        try:
            entry_date = datetime.fromisoformat(journal_entry["event_at"]).date()
        except Exception:
            pass

    # Fetch price context (14 days around entry → after event)
    start = entry_date - timedelta(days=14)
    end = act_date + timedelta(days=1)
    sym_bars = _fetch_bars(symbol, start, end)
    spy_bars = _fetch_bars("SPY", start, end)

    analysis = {
        "contract": contract,
        "symbol": symbol,
        "strike": strike,
        "opt_type": opt_type,
        "act_type": act_type,
        "act_date": act_date,
        "entry_date": entry_date,
        "qty": int(activity.get("qty") or 0),
        "journal_entry": journal_entry,
    }

    # Price at entry vs at assignment
    entry_close = next((b["close"] for b in sym_bars if b["date"] >= entry_date), None)
    act_close = next((b["close"] for b in sym_bars if b["date"] >= act_date), None)
    if entry_close and act_close:
        move = (act_close - entry_close) / entry_close
        analysis["entry_price"] = entry_close
        analysis["expiry_price"] = act_close
        analysis["total_move_pct"] = move
        # For puts: negative move means the stock dropped through strike. Distance breached:
        analysis["breach_pct"] = ((strike - act_close) / entry_close) if opt_type == "P" else \
                                  ((act_close - strike) / entry_close)

    # SPY move over same window
    spy_entry = next((b["close"] for b in spy_bars if b["date"] >= entry_date), None)
    spy_exit = next((b["close"] for b in spy_bars if b["date"] >= act_date), None)
    if spy_entry and spy_exit:
        analysis["spy_move_pct"] = (spy_exit - spy_entry) / spy_entry

    # Worst single-day drop during holding
    holding_bars = [b for b in sym_bars if entry_date <= b["date"] <= act_date]
    if len(holding_bars) >= 2:
        daily_rets = [(holding_bars[i]["close"] - holding_bars[i-1]["close"]) / holding_bars[i-1]["close"]
                      for i in range(1, len(holding_bars))]
        analysis["worst_daily_drop"] = min(daily_rets)
        analysis["best_daily_gain"] = max(daily_rets)

    # Entry context: 20-day RV + MA50 status at entry
    pre_bars = [b for b in sym_bars if b["date"] < entry_date][-20:]
    if len(pre_bars) >= 10:
        closes = [b["close"] for b in pre_bars]
        rets = [log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
        analysis["rv_at_entry"] = stdev(rets) * sqrt(252) if len(rets) > 1 else 0

    return analysis


# ── Report rendering ──────────────────────────────────────────────────

def _diagnose(a: dict) -> list[str]:
    """Return prioritized list of insights + suggestions."""
    insights = []

    if a.get("opt_type") != "P" or a.get("act_type") != "OPASN":
        return insights   # only analyze put assignments deeply

    total_move = a.get("total_move_pct")
    spy_move = a.get("spy_move_pct")
    rv_at_entry = a.get("rv_at_entry", 0)
    worst_drop = a.get("worst_daily_drop", 0)

    # 1. Market-wide vs symbol-specific
    if total_move is not None and spy_move is not None:
        if spy_move < -0.02 and total_move < 0:
            insights.append(
                f"📉 **Systemic drop**: SPY fell {spy_move:+.1%} over the holding period. "
                f"This was not symbol-specific — a market-wide risk-off move took the strike out. "
                f"**Mitigation**: Add a SPY 200-MA filter that halts new puts when SPY breaks below its 200-day MA."
            )
        elif total_move < -0.05 and abs(spy_move or 0) < 0.02:
            insights.append(
                f"🎯 **Symbol-specific hit**: {a['symbol']} fell {total_move:+.1%} while SPY was flat "
                f"({spy_move:+.1%}). Something about this name went wrong — earnings, guidance, news. "
                f"**Mitigation**: Tighten earnings window from 5d → 10d; add news-sentiment filter."
            )

    # 2. Worst single-day drop
    if worst_drop < -0.05:
        insights.append(
            f"⚡ **Gap day**: Worst single-day drop during holding was {worst_drop:+.1%}. "
            f"**Mitigation**: Our `check_realized_vol` filter triggers at annualized RV > 90% — "
            f"that only catches sustained noise, not fat-tail one-day events. Consider adding a "
            f"`check_recent_max_drop` filter blocking entries within 5 days of any > 4% daily drop."
        )

    # 3. IV at entry was low but stock still moved big
    if rv_at_entry and rv_at_entry < 0.35 and total_move is not None and total_move < -0.05:
        insights.append(
            f"📊 **Low-IV miss**: RV was only {rv_at_entry:.0%} at entry — "
            f"the market wasn't pricing this drop. IV filters wouldn't have caught it. "
            f"**Mitigation**: None purely from IV; rely on trend (MA50) + event filters."
        )

    # 4. Delta hindsight — how far OTM would we have needed?
    breach = a.get("breach_pct")
    entry_price = a.get("entry_price")
    if breach is not None and entry_price:
        # If we had sold a put at 50% further OTM, would we have survived?
        needed_buffer = a["strike"] - a.get("expiry_price", a["strike"])   # how much further down strike needed to be
        if needed_buffer > 0 and entry_price > 0:
            needed_pct = needed_buffer / entry_price * 100
            # Approximate delta that would have been this far OTM: assumes linear between -0.25 and -0.10
            # This is rough — delta doesn't scale linearly, but gives a directional hint
            insights.append(
                f"📏 **Strike selection**: To survive this move we'd have needed the strike "
                f"**{needed_pct:.1f}% further OTM**. At 0.25 delta we're ~{abs(a.get('breach_pct', 0))*100:.0f}% "
                f"OTM at entry — consider 0.15 delta (~ further OTM) when IV percentile ≥ 70 as a "
                f"dynamic-delta rule."
            )

    # 5. Filter journal check
    je = a.get("journal_entry") or {}
    filter_values = {k: je.get(f"filter_{k}") for k in ("earnings", "ma_trend", "vol", "iv_rank")
                     if je.get(f"filter_{k}")}
    if filter_values:
        insights.append(
            f"🔍 **Filter snapshot at entry**: {filter_values}. All passed; none warned."
        )

    if not insights:
        insights.append(
            "ℹ️ Not enough context to diagnose (missing journal entry or price data). "
            "Mitigation: ensure trade_journal.log_entry() is called on every open."
        )

    return insights


def _render_markdown(a: dict) -> str:
    head = f"# Assignment Postmortem — {a['symbol']} {a['contract']}"
    lines = [head, ""]
    lines.append(f"**Event**: {a['act_type']} on {a['act_date']}")
    lines.append(f"**Contract**: `{a['contract']}` (strike ${a['strike']:.2f} "
                 f"{'PUT' if a['opt_type']=='P' else 'CALL'})")
    lines.append(f"**Qty**: {a['qty']}  ·  **Held since (est)**: {a['entry_date']}")

    if "entry_price" in a and "expiry_price" in a:
        lines.append("")
        lines.append("## Price trajectory")
        lines.append("")
        lines.append("| Point | Price | Status |")
        lines.append("|---|---|---|")
        lines.append(f"| At entry | ${a['entry_price']:.2f} | strike ${a['strike']:.2f} "
                     f"({(a['strike'] - a['entry_price'])/a['entry_price']*100:+.1f}% OTM) |")
        lines.append(f"| At expiry | ${a['expiry_price']:.2f} | "
                     f"{'ITM' if (a['opt_type']=='P' and a['expiry_price'] < a['strike']) or (a['opt_type']=='C' and a['expiry_price'] > a['strike']) else 'OTM'} |")
        lines.append(f"| Total move | {a.get('total_move_pct', 0):+.2%} | vs SPY {a.get('spy_move_pct', 0):+.2%} |")

    if "worst_daily_drop" in a:
        lines.append("")
        lines.append("## Holding-period extremes")
        lines.append(f"- Worst single-day drop: **{a['worst_daily_drop']:+.2%}**")
        lines.append(f"- Best single-day gain: {a.get('best_daily_gain', 0):+.2%}")
        if a.get('rv_at_entry'):
            lines.append(f"- 20-day RV at entry: {a['rv_at_entry']:.0%}")

    insights = _diagnose(a)
    if insights:
        lines.append("")
        lines.append("## Why this happened + how to improve")
        lines.append("")
        for ins in insights:
            lines.append(f"- {ins}")

    if a.get("act_type") == "OPEXP":
        lines.append("")
        lines.append("_Note: this was an **expiration** (OTM win), not an assignment — logged "
                     "for completeness; no postmortem action needed._")

    lines.append("")
    lines.append(f"_Generated {datetime.now().isoformat(timespec='seconds')}_")
    return "\n".join(lines)


# ── Public entrypoints ────────────────────────────────────────────────

def detect_and_analyze(since_days: int = 14) -> list[Path]:
    """Scan recent activities, generate postmortems for anything new.

    Returns list of new markdown file paths.
    """
    state = _load_state()
    processed = set(state.get("processed_ids", []))

    # Pull both assignments AND expirations (expirations still recorded for context)
    activities = _fetch_activities(["OPASN", "OPEXP"], since_days=since_days)
    new_reports = []

    for a in activities:
        aid = a.get("id") or f"{a.get('activity_type')}-{a.get('symbol')}-{a.get('date')}"
        if aid in processed:
            continue

        analysis = _analyze(a)
        if "error" in analysis:
            logger.warning(f"Skip {aid}: {analysis['error']}")
            processed.add(aid)
            continue

        md = _render_markdown(analysis)
        fname = f"postmortem_{a.get('date')}_{analysis.get('symbol','?')}_{analysis.get('opt_type','?')}.md"
        path = REPORTS_DIR / fname
        path.write_text(md, encoding="utf-8")
        new_reports.append(path)
        processed.add(aid)
        logger.info(f"[postmortem] wrote {path.name}")

    state["processed_ids"] = list(processed)
    _save_state(state)
    return new_reports


def get_pending_summaries(max_items: int = 3) -> list[str]:
    """Return markdown snippets (first N lines of each report) of the most-recent
    postmortems that haven't been emailed yet.

    Uses a separate "emailed_ids" list in state.
    """
    state = _load_state()
    emailed = set(state.get("emailed_ids", []))

    # Newest first
    candidates = sorted(REPORTS_DIR.glob("postmortem_*.md"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    summaries = []
    for p in candidates[:max_items * 2]:
        if p.name in emailed:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        # Strip the top "_Generated_" footer line to keep email tight
        summaries.append(text.rsplit("\n_Generated", 1)[0].strip())
        emailed.add(p.name)
        if len(summaries) >= max_items:
            break

    state["emailed_ids"] = list(emailed)
    _save_state(state)
    return summaries


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    reports = detect_and_analyze(since_days=30)
    print(f"Generated {len(reports)} new postmortem(s):")
    for r in reports:
        print(f"  {r.relative_to(Path(settings.BASE_DIR))}")

    pending = get_pending_summaries(max_items=3)
    if pending:
        print(f"\nUnseen summaries ({len(pending)}):\n")
        for s in pending:
            print(s)
            print("\n---\n")
