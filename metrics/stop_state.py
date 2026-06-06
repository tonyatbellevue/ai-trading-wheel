"""Phase 4.1 — persistent state for the escalating stock stop-loss.

The bot runs stateless (each cron tick is a fresh process), so the
"N consecutive cycles still crashing → escalate to market" logic needs
its attempt-count persisted across runs.

State shape (JSON):
  { "MSFT": {"attempts": 2, "first_at": "2026-06-06T13:40:00"} , ... }

  attempts = how many cycles we've tried to stop-loss this symbol while
             it stayed below the -35% line WITH reliable data.
  Reset to 0 when price recovers above the line (handled by caller).
"""
from __future__ import annotations

import json
from pathlib import Path

from loguru import logger
from config import settings

STATE_FILE = Path(settings.BASE_DIR) / "metrics" / "data" / "stop_state.json"


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
        logger.debug(f"stop_state save failed: {e}")


def get_attempts(symbol: str) -> int:
    return int(_load().get(symbol, {}).get("attempts", 0))


def record_attempt(symbol: str, now_iso: str) -> int:
    """Increment and persist the attempt count. Returns the NEW count."""
    state = _load()
    entry = state.get(symbol, {"attempts": 0, "first_at": now_iso})
    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    if not entry.get("first_at"):
        entry["first_at"] = now_iso
    state[symbol] = entry
    _save(state)
    return entry["attempts"]


def reset(symbol: str) -> None:
    """Clear a symbol's stop state (price recovered or position closed)."""
    state = _load()
    if symbol in state:
        state.pop(symbol, None)
        _save(state)
