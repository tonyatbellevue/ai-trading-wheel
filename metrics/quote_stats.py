"""放行率监控 — tracks how often _get_option_fair_price returns a usable
price vs rejects (None). Answers the user's core question: "do all these
protection layers actually let the bot execute, or do they choke it?"

Each fair-price call records ONE outcome:
  - last        : used last-trade (priority 1)
  - mid         : used healthy mid (priority 2)
  - reject_bound: a candidate price was rejected by the intrinsic-value bound
  - reject_none : no reliable quote at all (priority 3 None)

Pass rate = (last + mid) / total. A HIGH pass rate (>90%) means the
protections don't choke normal execution. A LOW pass rate means we're
over-rejecting and SL/TP rarely fires — a red flag to loosen thresholds.

State is per-day, persisted to JSON (each cron run is a fresh process).
Reported daily by scripts/health_check.py.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from loguru import logger
from config import settings

STATE_FILE = Path(settings.BASE_DIR) / "metrics" / "data" / "quote_stats.json"
_OUTCOMES = ("last", "mid", "reject_bound", "reject_none")


def _load() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug(f"quote_stats save failed: {e}")


def record(outcome: str) -> None:
    """Record one fair-price outcome. outcome ∈ _OUTCOMES."""
    if outcome not in _OUTCOMES:
        return
    today = date.today().isoformat()
    state = _load()
    day = state.get(today) or {k: 0 for k in _OUTCOMES}
    day[outcome] = day.get(outcome, 0) + 1
    state[today] = day
    # Keep only last 14 days
    if len(state) > 14:
        for k in sorted(state.keys())[:-14]:
            state.pop(k, None)
    _save(state)


def summary(day: str | None = None) -> dict:
    """Return {total, used, pass_rate, breakdown} for a given day (default today)."""
    day = day or date.today().isoformat()
    state = _load()
    d = state.get(day) or {k: 0 for k in _OUTCOMES}
    used = d.get("last", 0) + d.get("mid", 0)
    total = used + d.get("reject_bound", 0) + d.get("reject_none", 0)
    rate = (used / total) if total else 1.0
    return {"day": day, "total": total, "used": used,
            "pass_rate": rate, "breakdown": d}
