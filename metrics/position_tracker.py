"""Unified position-change detection → single source of truth for exits.

Previously exits were logged from 3 different places (inside BTC code,
never for assignments, never for OTM expirations). That caused:
  - Duplicates when the same BTC re-ran across 5-min cron ticks
  - Missing exit rows for assignments (we saw the new stock position but
    never recorded the put's closure)
  - Delta-calibration and postmortem having incomplete data

This module snapshots the relevant positions every cron tick and diffs
them against the previous snapshot. Whatever disappeared gets a single,
correctly-reasoned exit row in trade_journal. This runs **before** the
usual run_cycle logic so the journal is always current before anyone
reads it.

Detection rules
  SHORT_PUT vanished + stock shares jumped by +100·qty  → `assigned`
  SHORT_PUT vanished + no stock change + past expiry    → `expired_otm`
  SHORT_PUT vanished + BTC order filled in last 24h     → `btc_50pct_profit`
  SHORT_CALL vanished + stock shares dropped by 100·qty → `assigned` (called away)
  SHORT_CALL vanished + stock shares unchanged          → `expired_otm`

For stock that leaves via CC assignment, compute P&L including both the
premium kept and the stock's realized gain vs. cost basis.
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings
from core.alpaca_client import AlpacaClients
from strategy.wheel_strategy import _parse_symbol

STATE_FILE = Path(settings.BASE_DIR) / "metrics" / "data" / "last_positions.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)


# ── Snapshot builder ───────────────────────────────────────────────────

def _snapshot_positions() -> dict:
    """Return a normalized dict of the current option shorts + stock position.

    Shape:
        {
            "timestamp": ISO,
            "options": {
                "CONTRACT_SYMBOL": {
                    "qty": -2, "entry": 2.38, "current": 0.03,
                    "underlying": "TSLA", "strike": 352.50,
                    "expiry": "2026-04-17", "type": "P"
                }, ...
            },
            "stocks": {"TSLA": {"qty": 100, "avg_entry": 352.50}, ...},
        }
    """
    snap = {
        "timestamp": datetime.now().isoformat(),
        "options": {},
        "stocks": {},
    }
    try:
        trading = AlpacaClients.trading()
        positions = trading.get_all_positions()
    except Exception as e:
        logger.warning(f"position snapshot failed: {e}")
        return snap

    for p in positions:
        info = _parse_symbol(p.symbol)
        if info:
            # Option position
            qty = float(p.qty)
            if qty >= 0:
                continue    # long option — not tracked
            snap["options"][p.symbol] = {
                "qty": qty,
                "entry": float(p.avg_entry_price),
                "current": float(p.current_price),
                "underlying": info["underlying"],
                "strike": info["strike"],
                "expiry": info["expiry"].isoformat(),
                "type": info["type"],
            }
        else:
            # Stock position
            snap["stocks"][p.symbol] = {
                "qty": float(p.qty),
                "avg_entry": float(p.avg_entry_price),
            }
    return snap


def _load_last() -> Optional[dict]:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(snap: dict) -> None:
    STATE_FILE.write_text(json.dumps(snap, indent=2, default=str),
                           encoding="utf-8")


# ── Diff → exit reason inference ──────────────────────────────────────

def _infer_exit_reason(contract: str, prev_opt: dict, curr_snap: dict,
                        last_snap: dict) -> tuple[str, float]:
    """Given an option that disappeared, figure out why and compute P&L.

    Returns (reason, pnl).
    """
    underlying = prev_opt["underlying"]
    opt_type = prev_opt["type"]
    strike = prev_opt["strike"]
    qty = abs(int(prev_opt["qty"]))
    entry_premium = prev_opt["entry"]

    prev_stock = last_snap.get("stocks", {}).get(underlying, {})
    curr_stock = curr_snap.get("stocks", {}).get(underlying, {})
    prev_shares = prev_stock.get("qty", 0)
    curr_shares = curr_stock.get("qty", 0)
    share_diff = curr_shares - prev_shares

    # ─ PUT disappeared ─
    if opt_type == "P":
        # Assignment: shares jumped by +100·qty
        if share_diff >= 100 * qty - 1:   # small tolerance
            # P&L = premium kept (intrinsic loss already realized in stock cost basis)
            pnl = entry_premium * 100 * qty
            # But the stock is now underwater — record only the premium as realized;
            # the stock loss is unrealized until we exit the LONG_STOCK phase.
            return "assigned", pnl

        # BTC filled: check for a recent buy-side fill on this contract
        if _recent_btc_fill(contract):
            # P&L = entry - actual_fill_price (we approximate with prev current)
            close_price = prev_opt.get("current", 0)
            pnl = (entry_premium - close_price) * 100 * qty
            return "btc_50pct_profit", pnl

        # Otherwise: expired OTM (full premium kept)
        return "expired_otm", entry_premium * 100 * qty

    # ─ CALL disappeared ─
    else:
        # Called away: shares dropped by 100·qty
        if share_diff <= -100 * qty + 1:
            # Realized = premium + stock gain (strike - cost_basis) * shares
            premium_pnl = entry_premium * 100 * qty
            # Cost basis we recorded last snapshot
            cost = prev_stock.get("avg_entry", strike)
            stock_pnl = (strike - cost) * 100 * qty
            return "assigned", premium_pnl + stock_pnl

        if _recent_btc_fill(contract):
            close_price = prev_opt.get("current", 0)
            pnl = (entry_premium - close_price) * 100 * qty
            return "btc_50pct_profit", pnl

        return "expired_otm", entry_premium * 100 * qty


def _recent_btc_fill(contract_symbol: str, hours: int = 24) -> bool:
    """Check Alpaca closed orders for a recent BUY on this contract."""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        trading = AlpacaClients.trading()
        orders = trading.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, limit=50,
        ))
        cutoff = datetime.now().astimezone() - timedelta(hours=hours)
        for o in orders:
            if o.symbol != contract_symbol:
                continue
            if not str(o.side).endswith("BUY"):
                continue
            if not o.filled_at:
                continue
            filled_at = o.filled_at if o.filled_at.tzinfo else o.filled_at.replace(tzinfo=cutoff.tzinfo)
            if filled_at >= cutoff:
                return True
    except Exception as e:
        logger.debug(f"_recent_btc_fill check failed: {e}")
    return False


# ── Public entrypoint ─────────────────────────────────────────────────

def reconcile() -> list[dict]:
    """Snapshot current positions, diff against last snapshot, log any
    disappearances to trade_journal. Returns the list of events logged.
    """
    curr = _snapshot_positions()
    last = _load_last()

    events = []
    if last is None:
        # First run — just save baseline, don't log anything
        _save(curr)
        return events

    last_contracts = set(last.get("options", {}).keys())
    curr_contracts = set(curr.get("options", {}).keys())
    vanished = last_contracts - curr_contracts

    if not vanished:
        _save(curr)
        return events

    try:
        from metrics.trade_journal import log_exit
    except Exception as e:
        logger.warning(f"trade_journal import failed: {e}")
        _save(curr)
        return events

    for contract in vanished:
        prev_opt = last["options"][contract]
        underlying = prev_opt["underlying"]
        qty = abs(int(prev_opt["qty"]))
        reason, pnl = _infer_exit_reason(contract, prev_opt, curr, last)

        log_exit(
            symbol=underlying, contract=contract, qty=qty,
            pnl=pnl, exit_reason=reason,
            notes=f"detected by position_tracker; entry=${prev_opt['entry']:.2f}",
        )
        events.append({
            "contract": contract, "reason": reason, "pnl": pnl,
            "underlying": underlying, "qty": qty,
        })
        logger.info(f"[tracker] {contract} → {reason} (P&L ${pnl:+,.2f})")

    _save(curr)
    return events


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    events = reconcile()
    if events:
        print(f"Logged {len(events)} exit event(s):")
        for e in events:
            print(f"  {e['contract']}: {e['reason']} P&L ${e['pnl']:+,.2f}")
    else:
        print("No position changes detected.")
