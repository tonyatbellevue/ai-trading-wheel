"""Multi-wheel scenario simulator — prevents MSFT-style orphan-position bugs.

Why this exists
---------------
On 6/3 MSFT 6/3 $442.5P went deep ITM and lost $1,418. Root cause was a v12
multi-wheel bug: `wheel_once.py` only ran `WheelStrategy(primary)` + one new
candidate, so any *secondary* position (a wheel that had been picked up earlier)
became an orphan — `_try_stop_loss` and `_try_take_profit` never fired for it.

PR #99 fixed `wheel_once.main()` to enumerate ALL existing positions and call
`WheelStrategy(sym).run_cycle()` for each one. This script is the regression
harness that proves the fix works, by simulating every scenario the bug could
have lived in.

How it works
------------
We do NOT hit Alpaca. We monkey-patch:
  - `AlpacaClients.trading()`   → FakeTradingClient (returns scripted positions/orders)
  - `OptionOrderManager`        → FakeOrderManager  (records calls)
  - `wheel_evaluator._find_best_alternative` → returns a scripted symbol
  - `WheelStrategy.run_cycle`   → records which symbols were invoked

Then we call `wheel_once.main()` and assert which wheels got `run_cycle()` and
which BTC/CC/STO orders the strategy code would have placed.

Run
---
    python scripts/simulate_multi_wheel.py           # all scenarios
    python scripts/simulate_multi_wheel.py A C       # only scenarios A and C

Exit code 0 → all scenarios pass.  Non-zero → at least one regression.
"""
from __future__ import annotations

import sys
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch, MagicMock

# Make `import wheel_once`, `from strategy...` etc. work from this script.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ──────────────────────────────────────────────────────────────────────────
# Fake Alpaca objects — minimal duck-typing of the real SDK shapes.
# ──────────────────────────────────────────────────────────────────────────

def _occ(symbol: str, expiry: date, strike: float, opt_type: str) -> str:
    """Build an OCC option code, e.g. MSFT250603P00442500."""
    yymmdd = expiry.strftime("%y%m%d")
    strike_str = f"{int(round(strike * 1000)):08d}"
    return f"{symbol}{yymmdd}{opt_type}{strike_str}"


@dataclass
class FakePosition:
    symbol: str
    qty: float                     # negative for short option, positive for long stock
    avg_entry_price: float
    current_price: float
    unrealized_pl: float = 0.0


@dataclass
class FakeOrder:
    id: str
    symbol: str
    qty: int
    side: str                       # "SELL" or "BUY"
    limit_price: float
    status: str = "OPEN"


@dataclass
class FakeClock:
    is_open: bool = True
    next_open: object = None


class FakeTradingClient:
    """Mimics alpaca.trading.client.TradingClient for our subset of usage."""

    def __init__(self, positions=None, orders=None, account=None):
        self._positions = positions or []
        self._orders = orders or []
        self._account = account or SimpleNamespace(
            equity="100000", last_equity="100000",
            cash="80000", buying_power="160000",
        )

    def get_clock(self):
        return FakeClock(is_open=True)

    def get_all_positions(self):
        return list(self._positions)

    def get_open_position(self, symbol):
        for p in self._positions:
            if p.symbol == symbol:
                return p
        raise Exception(f"position not found: {symbol}")

    def get_orders(self, filter=None):
        return list(self._orders)

    def get_account(self):
        return self._account


# ──────────────────────────────────────────────────────────────────────────
# Recorder — every run_cycle() invocation lands here.
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class Recorder:
    run_cycle_calls: list = field(default_factory=list)
    btc_calls: list = field(default_factory=list)         # (symbol, qty, limit)
    sto_calls: list = field(default_factory=list)
    cycle_actions: dict = field(default_factory=dict)     # symbol → fake "what would happen"

    def reset(self):
        self.run_cycle_calls.clear()
        self.btc_calls.clear()
        self.sto_calls.clear()
        self.cycle_actions.clear()


REC = Recorder()


def _fake_run_cycle(self):
    """Replacement for WheelStrategy.run_cycle that records intent
    instead of hitting Alpaca. We DO call get_phase() and the
    risk-management helpers so the real code paths are exercised.
    """
    REC.run_cycle_calls.append(self.symbol)
    try:
        phase, obj = self.get_phase()
    except Exception as e:
        REC.cycle_actions[self.symbol] = f"get_phase FAILED: {e}"
        return
    action = {"phase": phase.name, "stop_loss_fired": False,
              "take_profit_fired": False, "would_sell_cc": False}

    if phase.name in ("SHORT_PUT", "SHORT_CALL"):
        # Mirror the real run_cycle order: stop-loss first, then TP.
        try:
            if self._try_stop_loss(obj):
                action["stop_loss_fired"] = True
        except Exception as e:
            action["stop_loss_error"] = str(e)
        try:
            if not action["stop_loss_fired"] and self._try_take_profit(obj):
                action["take_profit_fired"] = True
        except Exception as e:
            action["take_profit_error"] = str(e)
    elif phase.name == "LONG_STOCK":
        # Mirror the REAL LONG_STOCK branch from wheel_strategy.run_cycle:
        #   1. pre_open_call_checks(symbol)   — earnings filter
        #   2. cost_basis from position
        #   3. contracts = qty // 100         — partial assignment guard
        #   4. select_call(cost_basis)        — strike ≥ cost basis
        #   5. sell_to_open(...)
        from strategy.wheel_filters import pre_open_call_checks
        from strategy import wheel_strategy as _ws
        action["cost_basis"] = float(obj.avg_entry_price)
        action["qty"] = float(obj.qty)

        ok, reason = pre_open_call_checks(self.symbol)
        action["earnings_check"] = (ok, reason)
        if not ok:
            action["cc_skipped_reason"] = f"earnings: {reason}"
            REC.cycle_actions[self.symbol] = action
            return

        contracts = int(float(obj.qty) // 100)
        if contracts < 1:
            action["cc_skipped_reason"] = f"qty {obj.qty} < 100"
            REC.cycle_actions[self.symbol] = action
            return

        # Call select_call so the cost-basis-strike constraint is exercised.
        # In test mode this is patched per-scenario to return a chosen contract.
        try:
            result = self.select_call(float(obj.avg_entry_price))
        except Exception as e:
            action["cc_skipped_reason"] = f"select_call exception: {e}"
            REC.cycle_actions[self.symbol] = action
            return

        if not result:
            action["cc_skipped_reason"] = "no_call_at_or_above_cost"
            REC.cycle_actions[self.symbol] = action
            return

        sym, mid, delta = result
        action["would_sell_cc"] = True
        action["cc_strike"] = _ws._parse_symbol(sym).get("strike")
        action["cc_premium"] = mid
        action["cc_contracts"] = contracts
        self._option_mgr.sell_to_open(sym, contracts, mid)
    elif phase.name == "IDLE":
        action["would_sell_csp"] = True

    REC.cycle_actions[self.symbol] = action


# ──────────────────────────────────────────────────────────────────────────
# Scenario builders
# ──────────────────────────────────────────────────────────────────────────

today = date(2026, 6, 4)
next_fri = today + timedelta(days=(4 - today.weekday()) % 7 or 7)


def scenario_A():
    """A: 2 short puts, both losing. Deep one (-200%) must trigger stop-loss."""
    gm_sym  = _occ("GM",  next_fri, 78.0, "P")
    msft_sym = _occ("MSFT", next_fri, 442.5, "P")
    positions = [
        # GM mild loser: current 1.15x entry → no stop-loss yet
        FakePosition(gm_sym, qty=-1, avg_entry_price=0.50, current_price=0.58),
        # MSFT deep loser: current 5x entry → MUST stop-loss
        FakePosition(msft_sym, qty=-1, avg_entry_price=1.00, current_price=5.00),
    ]
    return positions, [], "GM", {
        "wheels_managed": {"GM", "MSFT"},
        "must_stop_loss": {msft_sym},
        "must_not_stop_loss": {gm_sym},
    }


def scenario_B():
    """B: 1 assigned (LONG_STOCK MSFT), 1 short put (GM) — both must be managed."""
    gm_sym = _occ("GM", next_fri, 78.0, "P")
    positions = [
        FakePosition(gm_sym, qty=-1, avg_entry_price=0.50, current_price=0.20),  # near TP
        FakePosition("MSFT", qty=100, avg_entry_price=442.5, current_price=427.0),
    ]
    return positions, [], "GM", {
        "wheels_managed": {"GM", "MSFT"},
        "long_stock": {"MSFT"},
    }


def scenario_C():
    """C: 3 wheels (GM short put + MSFT 100 shares + AVGO short put). MAX=2.
       All 3 must still be managed (cap only limits NEW candidates)."""
    gm_sym = _occ("GM", next_fri, 78.0, "P")
    avgo_sym = _occ("AVGO", next_fri, 240.0, "P")
    positions = [
        FakePosition(gm_sym, qty=-1, avg_entry_price=0.50, current_price=0.40),
        FakePosition("MSFT", qty=100, avg_entry_price=442.5, current_price=440.0),
        FakePosition(avgo_sym, qty=-1, avg_entry_price=1.20, current_price=1.10),
    ]
    return positions, [], "GM", {"wheels_managed": {"GM", "MSFT", "AVGO"}}


def scenario_D():
    """D: idle account — only primary should be managed, plus 1 new candidate."""
    return [], [], "GM", {"wheels_managed_min": {"GM"}}


def scenario_E():
    """E: malformed OCC symbol present — must not crash, must skip it."""
    gm_sym = _occ("GM", next_fri, 78.0, "P")
    bogus = FakePosition("THIS_IS_NOT_OCC_FORMAT_X", qty=-1,
                          avg_entry_price=1.0, current_price=1.0)
    positions = [
        FakePosition(gm_sym, qty=-1, avg_entry_price=0.50, current_price=0.20),
        bogus,
    ]
    return positions, [], "GM", {"wheels_managed_subset": {"GM"}}


def scenario_F():
    """F: same symbol appears as option short + stock long
       (rare but possible during assignment overlap) — must not duplicate."""
    msft_put = _occ("MSFT", next_fri, 442.5, "P")
    positions = [
        FakePosition(msft_put, qty=-1, avg_entry_price=1.0, current_price=5.0),
        FakePosition("MSFT", qty=100, avg_entry_price=442.5, current_price=427.0),
    ]
    return positions, [], "GM", {
        "wheels_managed": {"GM", "MSFT"},
        "no_duplicate_calls": True,
    }


def scenario_G():
    """G: MAX_CONCURRENT_WHEELS=1 mode — existing positions still ALL managed."""
    gm_sym = _occ("GM", next_fri, 78.0, "P")
    msft_put = _occ("MSFT", next_fri, 442.5, "P")
    positions = [
        FakePosition(gm_sym, qty=-1, avg_entry_price=0.50, current_price=0.40),
        FakePosition(msft_put, qty=-1, avg_entry_price=1.0, current_price=5.0),
    ]
    return positions, [], "GM", {
        "wheels_managed": {"GM", "MSFT"},
        "max_concurrent_override": 1,
        "must_stop_loss": {msft_put},
    }


def scenario_H():
    """H: post-assignment first cycle — MSFT just got assigned, no put left,
       100 shares freshly held. Must enter LONG_STOCK phase and (in real code)
       sell a CC at strike ≥ cost basis."""
    positions = [
        FakePosition("MSFT", qty=100, avg_entry_price=442.5, current_price=440.0),
    ]
    return positions, [], "MSFT", {
        "wheels_managed": {"MSFT"},
        "long_stock": {"MSFT"},
    }


# ──────────────────────────────────────────────────────────────────────────
# Post-assignment scenarios (I–P) — every path the bot walks AFTER an
# OCC overnight exercise. These are the ones MSFT 6/3 would have hit
# the morning after assignment, had PR #99 not fixed wheel_once.
# ──────────────────────────────────────────────────────────────────────────


def _mk_call(symbol, strike, expiry=None):
    """Return a fake (sym, mid, delta) tuple as select_call would."""
    exp = expiry or next_fri
    return (_occ(symbol, exp, strike, "C"), 2.50, 0.25)


def scenario_I_basic_cc_sale():
    """I: Fresh assignment, normal market — sells CC at strike ≥ cost basis."""
    positions = [
        FakePosition("MSFT", qty=100, avg_entry_price=442.5, current_price=445.0),
    ]
    return positions, [], "MSFT", {
        "wheels_managed": {"MSFT"},
        "select_call_returns": {"MSFT": _mk_call("MSFT", 445.0)},  # strike > cost
        "earnings_pass": True,
        "must_sell_cc": "MSFT",
        "must_cc_strike_ge": ("MSFT", 442.5),  # 行权价不能 < cost basis
    }


def scenario_J_earnings_blocks_cc():
    """J: Just assigned, but earnings in 5 days → MUST skip CC sale."""
    positions = [
        FakePosition("MSFT", qty=100, avg_entry_price=442.5, current_price=445.0),
    ]
    return positions, [], "MSFT", {
        "wheels_managed": {"MSFT"},
        "earnings_pass": False,
        "earnings_reason": "earnings in 5d (mocked)",
        "must_not_sell_cc": "MSFT",
        "expect_skip_reason_contains": "earnings",
    }


def scenario_K_partial_assignment():
    """K: User has only 80 shares (partial fill or mis-assignment) — < 100,
       must NOT attempt to sell CC."""
    positions = [
        FakePosition("MSFT", qty=80, avg_entry_price=442.5, current_price=445.0),
    ]
    return positions, [], "MSFT", {
        # 80 shares < 100, get_phase will NOT enter LONG_STOCK (it checks ≥100).
        # So the loop will treat MSFT as IDLE-on-stock — must not crash and
        # must not sell a CC.
        "wheels_managed_subset": {"MSFT"},
        "must_not_sell_cc": "MSFT",
    }


def scenario_L_underwater_no_safe_cc():
    """L: Stock dropped well below cost (MSFT $410 vs $442.5 cost basis).
       select_call returns None because no strike ≥ cost has decent premium —
       must skip rather than lock in a loss."""
    positions = [
        FakePosition("MSFT", qty=100, avg_entry_price=442.5, current_price=410.0),
    ]
    return positions, [], "MSFT", {
        "wheels_managed": {"MSFT"},
        "earnings_pass": True,
        "select_call_returns": {"MSFT": None},  # no chain match ≥ cost
        "must_not_sell_cc": "MSFT",
        "expect_skip_reason_contains": "no_call_at_or_above_cost",
    }


def scenario_M_two_assignments_same_week():
    """M: Both MSFT 100 and GM 100 got assigned this week. Both go LONG_STOCK
       and each must independently attempt a CC sale."""
    positions = [
        FakePosition("MSFT", qty=100, avg_entry_price=442.5, current_price=445.0),
        FakePosition("GM",   qty=100, avg_entry_price=78.0,  current_price=79.0),
    ]
    return positions, [], "MSFT", {
        "wheels_managed": {"MSFT", "GM"},
        "earnings_pass": True,
        "select_call_returns": {
            "MSFT": _mk_call("MSFT", 445.0),
            "GM":   _mk_call("GM",   80.0),
        },
        "must_sell_cc_each": {"MSFT", "GM"},
        "must_cc_strike_ge": ("MSFT", 442.5),
    }


def scenario_N_cc_stop_loss():
    """N: Already sold CC, stock rallied hard, CC now at 4× entry — stop-loss."""
    cc_sym = _occ("MSFT", next_fri, 445.0, "C")
    positions = [
        FakePosition("MSFT", qty=100, avg_entry_price=442.5, current_price=460.0),
        FakePosition(cc_sym, qty=-1, avg_entry_price=2.00, current_price=8.00),  # 4×
    ]
    return positions, [], "MSFT", {
        "wheels_managed": {"MSFT"},
        "must_stop_loss": {cc_sym},
    }


def scenario_O_cc_take_profit():
    """O: Already sold CC, decayed to ~$0.50 from $2.00 — 75% profit, take-profit."""
    # DTE must be ≥ 6 so v11 hold doesn't kick in (it skips BTC when DTE 2-5)
    far_exp = today + timedelta(days=10)
    cc_sym = _occ("MSFT", far_exp, 445.0, "C")
    positions = [
        FakePosition("MSFT", qty=100, avg_entry_price=442.5, current_price=443.0),
        FakePosition(cc_sym, qty=-1, avg_entry_price=2.00, current_price=0.50),  # 75% profit
    ]
    return positions, [], "MSFT", {
        "wheels_managed": {"MSFT"},
        "must_take_profit": {cc_sym},
    }


def scenario_Q_current_account_shape():
    """Q: REAL CURRENT ACCOUNT SHAPE (2026-06-04 SGT 22:00).
       GM: -6 puts (multi-contract, not just 1!)
       MSFT: 100 shares LONG + 1 CC already sold (SHORT_CALL phase)
       Verifies bot manages BOTH wheels correctly with no spurious new orders.

       Expected: both wheels enter monitor-only mode. No new STO, no BTC.
       (GM puts profitable but not at 65% TP, MSFT CC slightly underwater
        but well within stop-loss threshold.)
    """
    gm_put = _occ("GM", today + timedelta(days=1), 78.0, "P")
    msft_cc = _occ("MSFT", today + timedelta(days=4), 442.5, "C")
    positions = [
        FakePosition(gm_put, qty=-6, avg_entry_price=0.26, current_price=0.17),
        FakePosition(msft_cc, qty=-1, avg_entry_price=1.99, current_price=2.20),
        FakePosition("MSFT", qty=100, avg_entry_price=441.13, current_price=431.74),
    ]
    return positions, [], "GM", {
        "wheels_managed": {"GM", "MSFT"},
        "must_not_stop_loss": {gm_put, msft_cc},
        "must_not_sell_cc": "MSFT",
    }


def scenario_T_phase1_skip_on_bad_quote():
    """T: Phase 1 GM-loss prevention.
       Position would trigger SL by mark ($0.78 = 3x entry $0.26), but
       fair_price returns None (simulating bid=0, stale quote, or NBBO
       unhealthy). Bot must SKIP, not fall back to mark.

       This is the exact GM 6/5 scenario in reverse: with the Phase 1
       fix, the $1,122 loss would have been $0.
    """
    gm_put = _occ("GM", today + timedelta(days=1), 78.0, "P")
    positions = [
        FakePosition(gm_put, qty=-6, avg_entry_price=0.26, current_price=0.78),
    ]
    return positions, [], "GM", {
        "wheels_managed": {"GM"},
        # CRITICAL: even though mark = 3x entry, fair_price=None → SKIP
        "must_not_stop_loss": {gm_put},
        # Override the default fair_price mock for THIS scenario only
        "_force_fair_price_none": True,
    }


def scenario_R_pending_switch_triggers():
    """R: PENDING-SWITCH PLAN consumption (Monday 6/8 morning).
       Models the situation after GM puts expire Friday and trigger_date
       6/7 has passed. GM is IDLE, MSFT still has CC active.

       Critical: the active plan in wheel_symbol.json (planned 6/4) says
       MSFT → GM @ 6/7. But maybe_switch() does NOT check from_symbol —
       it uses active_symbol as old_symbol. So when GM (active) is IDLE
       and date >= trigger, the plan fires and rescan picks top-1
       (currently NVDA score 92.8).

       Expected: primary rotates GM → NVDA, MSFT continues as secondary.
       The bot must still manage MSFT (orphan-management — PR #99).
    """
    msft_cc = _occ("MSFT", today + timedelta(days=4), 442.5, "C")
    positions = [
        # GM puts gone (expired Friday OTM) — no GM option position
        # MSFT CC still active
        FakePosition(msft_cc, qty=-1, avg_entry_price=1.99, current_price=2.20),
        FakePosition("MSFT", qty=100, avg_entry_price=441.13, current_price=431.74),
    ]
    return positions, [], "GM", {
        # Must manage both — even if primary GM has no position, MSFT
        # secondary must still run (PR #99 orphan-management fix).
        "wheels_managed": {"GM", "MSFT"},
        # MSFT CC unchanged — no spurious new CC since SHORT_CALL active
        "must_not_sell_cc": "MSFT",
        # MSFT CC mildly underwater but not at 3× stop-loss
        "must_not_stop_loss": {msft_cc},
    }


def scenario_P_cc_expired_back_to_long_stock():
    """P: CC expired worthless overnight, only stock remains — must re-enter
       LONG_STOCK and sell a NEW CC. Tests the wheel actually rotates."""
    positions = [
        FakePosition("MSFT", qty=100, avg_entry_price=442.5, current_price=443.0),
    ]
    return positions, [], "MSFT", {
        "wheels_managed": {"MSFT"},
        "earnings_pass": True,
        "select_call_returns": {"MSFT": _mk_call("MSFT", 445.0)},
        "must_sell_cc": "MSFT",
    }


SCENARIOS = {
    "A": ("2 short puts, deep loser must stop-loss", scenario_A),
    "B": ("assigned + active put — manage both", scenario_B),
    "C": ("3 wheels, MAX=2 — manage all 3 existing", scenario_C),
    "D": ("idle account — manage primary only", scenario_D),
    "E": ("malformed OCC symbol — skip gracefully", scenario_E),
    "F": ("same underlying option + stock — no duplicate run_cycle", scenario_F),
    "G": ("MAX=1 mode — orphans still managed", scenario_G),
    "H": ("post-assignment — LONG_STOCK phase recognised", scenario_H),
    # ── Post-assignment deep coverage ──
    "I": ("post-asn: sells CC at strike ≥ cost basis", scenario_I_basic_cc_sale),
    "J": ("post-asn: earnings within 7d blocks CC sale", scenario_J_earnings_blocks_cc),
    "K": ("post-asn: <100 shares → no CC attempt", scenario_K_partial_assignment),
    "L": ("post-asn: underwater, no safe strike → skip CC (don't lock loss)",
          scenario_L_underwater_no_safe_cc),
    "M": ("post-asn: 2 same-week assignments — both sell CC independently",
          scenario_M_two_assignments_same_week),
    "N": ("post-asn: CC blown out (4× entry) → stop-loss BTC", scenario_N_cc_stop_loss),
    "O": ("post-asn: CC decayed 75% → take-profit BTC", scenario_O_cc_take_profit),
    "P": ("post-asn: CC expired, back to LONG_STOCK, sells new CC",
          scenario_P_cc_expired_back_to_long_stock),
    "Q": ("REAL ACCOUNT (6/4): GM -6 puts + MSFT 100sh + MSFT -1 CC → no spurious orders",
          scenario_Q_current_account_shape),
    "R": ("Mon 6/8 path: GM puts expired + plan triggers + MSFT CC still active",
          scenario_R_pending_switch_triggers),
    "T": ("Phase 1: bad quote (fair=None) → SKIP SL even if mark=3x — saves GM",
          scenario_T_phase1_skip_on_bad_quote),
}


# ──────────────────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────────────────

def _install_mocks(positions, orders, primary, max_concurrent_override=None,
                    select_call_returns=None, earnings_pass=True,
                    earnings_reason="OK", force_fair_price_none=False):
    """Patch the modules wheel_once depends on. Returns context managers."""
    from core import alpaca_client
    from execution import option_order_manager as oom_mod
    from strategy import wheel_strategy, wheel_evaluator
    from config import settings

    fake_client = FakeTradingClient(positions=positions, orders=orders)

    # 1. Trading client
    p1 = patch.object(alpaca_client.AlpacaClients, "trading",
                      classmethod(lambda cls: fake_client))
    # 2. Order manager — replace its underlying client and methods we'd call.
    fake_oom = MagicMock(spec=oom_mod.OptionOrderManager)
    fake_oom.get_option_positions.side_effect = lambda u: [
        p for p in positions if p.symbol.startswith(u) and len(p.symbol) > 10
    ]
    fake_oom.get_open_option_orders.side_effect = lambda u: [
        o for o in orders if o.symbol.startswith(u) and len(o.symbol) > 10
    ]
    fake_oom.buy_to_close.side_effect = lambda sym, q, lim: (
        REC.btc_calls.append((sym, q, lim)) or "fake-btc-id"
    )
    fake_oom.sell_to_open.side_effect = lambda sym, q, lim: (
        REC.sto_calls.append((sym, q, lim)) or "fake-sto-id"
    )
    p2 = patch.object(oom_mod, "OptionOrderManager", lambda: fake_oom)
    # WheelStrategy imports OptionOrderManager at module-top, so we also
    # need to patch the re-export inside strategy.wheel_strategy.
    p2b = patch.object(wheel_strategy, "OptionOrderManager", lambda: fake_oom)

    # 3. New-candidate picker — keep it deterministic
    p3 = patch.object(wheel_evaluator, "_find_best_alternative",
                      lambda exclude=None: "NEWCAND")
    # 4. WheelStrategy.run_cycle → recorder shim
    p4 = patch.object(wheel_strategy.WheelStrategy, "run_cycle", _fake_run_cycle)
    # 4b. _get_option_fair_price → return mocked position's current_price.
    # Phase 1 fix changed semantics: returning None now means SKIP SL/TP,
    # so old "return None" patch broke SL tests. Now return current_price
    # from the matching FakePosition (treat mock as "reliable fair value").
    def _fake_fair_price(self, sym):
        if force_fair_price_none:
            return None   # scenario T: simulate bad-quote conditions
        for p in positions:
            if p.symbol == sym:
                return float(p.current_price)
        return None
    p4b = patch.object(wheel_strategy.WheelStrategy, "_get_option_fair_price",
                        _fake_fair_price)

    # 5. settings.WHEEL_SYMBOL
    p5 = patch.object(settings, "WHEEL_SYMBOL", primary)

    patches = [p1, p2, p2b, p3, p4, p4b, p5]
    if max_concurrent_override is not None:
        patches.append(patch.object(settings, "MAX_CONCURRENT_WHEELS",
                                     max_concurrent_override))

    # 6. select_call — per-symbol scripted return (LONG_STOCK scenarios)
    select_call_returns = select_call_returns or {}
    def _fake_select_call(self, cost_basis):
        return select_call_returns.get(self.symbol)
    patches.append(patch.object(wheel_strategy.WheelStrategy, "select_call",
                                 _fake_select_call))

    # 7. pre_open_call_checks — scripted earnings filter
    from strategy import wheel_filters
    patches.append(patch.object(
        wheel_filters, "pre_open_call_checks",
        lambda sym: (earnings_pass, "" if earnings_pass else earnings_reason)
    ))
    return patches


def run_scenario(label, desc, builder):
    positions, orders, primary, expect = builder()
    REC.reset()

    max_override = expect.get("max_concurrent_override")
    patches = _install_mocks(
        positions, orders, primary, max_override,
        select_call_returns=expect.get("select_call_returns"),
        earnings_pass=expect.get("earnings_pass", True),
        earnings_reason=expect.get("earnings_reason", "OK"),
        force_fair_price_none=expect.get("_force_fair_price_none", False),
    )

    for p in patches:
        p.start()
    try:
        import wheel_once
        try:
            wheel_once.main()
        except SystemExit as e:
            # main() calls sys.exit(0) when market closed — but we mock it open
            if e.code not in (0, None):
                return False, f"main() exited with code {e.code}"
    finally:
        for p in patches:
            p.stop()

    # ── Assertions ────────────────────────────────────────────────
    failures = []
    called = set(REC.run_cycle_calls)

    if "wheels_managed" in expect:
        missing = expect["wheels_managed"] - called
        if missing:
            failures.append(f"missing run_cycle on {missing}")

    if "wheels_managed_min" in expect:
        missing = expect["wheels_managed_min"] - called
        if missing:
            failures.append(f"missing run_cycle on {missing}")

    if "wheels_managed_subset" in expect:
        missing = expect["wheels_managed_subset"] - called
        if missing:
            failures.append(f"missing run_cycle on {missing}")

    if expect.get("no_duplicate_calls"):
        if len(REC.run_cycle_calls) != len(set(REC.run_cycle_calls)):
            failures.append(
                f"duplicate run_cycle calls: {REC.run_cycle_calls}"
            )

    if "must_stop_loss" in expect:
        sl_symbols = {sym for sym, _, _ in REC.btc_calls}
        missing = expect["must_stop_loss"] - sl_symbols
        if missing:
            failures.append(
                f"stop-loss should have fired on {missing} "
                f"(actual BTC calls: {REC.btc_calls})"
            )

    if "must_not_stop_loss" in expect:
        sl_symbols = {sym for sym, _, _ in REC.btc_calls}
        unwanted = expect["must_not_stop_loss"] & sl_symbols
        if unwanted:
            failures.append(f"stop-loss should NOT have fired on {unwanted}")

    if "long_stock" in expect:
        for sym in expect["long_stock"]:
            act = REC.cycle_actions.get(sym, {})
            if act.get("phase") != "LONG_STOCK":
                failures.append(
                    f"{sym} expected LONG_STOCK phase, got {act.get('phase')}"
                )

    # ── Post-assignment / CC-specific assertions ──
    sto_symbols = {sym for sym, _, _ in REC.sto_calls}

    if "must_sell_cc" in expect:
        sym = expect["must_sell_cc"]
        act = REC.cycle_actions.get(sym, {})
        if not act.get("would_sell_cc"):
            failures.append(
                f"{sym} should have sold CC but didn't "
                f"(skip_reason={act.get('cc_skipped_reason', 'unknown')})"
            )
        elif not any(s.startswith(sym) for s in sto_symbols):
            failures.append(f"{sym} would_sell_cc=True but no sell_to_open call")

    if "must_sell_cc_each" in expect:
        for sym in expect["must_sell_cc_each"]:
            act = REC.cycle_actions.get(sym, {})
            if not act.get("would_sell_cc"):
                failures.append(
                    f"{sym} should have sold CC but didn't "
                    f"(skip_reason={act.get('cc_skipped_reason', 'unknown')})"
                )

    if "must_not_sell_cc" in expect:
        sym = expect["must_not_sell_cc"]
        act = REC.cycle_actions.get(sym, {})
        if act.get("would_sell_cc"):
            failures.append(f"{sym} should NOT have sold CC but did")
        if any(s.startswith(sym) and "C" in s[-9:-8] for s in sto_symbols):
            failures.append(f"{sym} CC sell_to_open fired but must_not_sell_cc")

    if "must_cc_strike_ge" in expect:
        sym, min_strike = expect["must_cc_strike_ge"]
        act = REC.cycle_actions.get(sym, {})
        cs = act.get("cc_strike")
        if cs is not None and cs < min_strike:
            failures.append(
                f"{sym} CC strike {cs} < cost basis {min_strike} — locking loss!"
            )

    if "expect_skip_reason_contains" in expect:
        # Find any symbol whose skip reason contains the substring
        substr = expect["expect_skip_reason_contains"]
        found = any(substr in (act.get("cc_skipped_reason") or "")
                    for act in REC.cycle_actions.values())
        if not found:
            failures.append(
                f"expected a CC skip with reason containing '{substr}', "
                f"actions={REC.cycle_actions}"
            )

    if "must_take_profit" in expect:
        # Take-profit results in a BTC order with limit price < entry
        tp_symbols = {sym for sym, _, _ in REC.btc_calls}
        missing = expect["must_take_profit"] - tp_symbols
        if missing:
            failures.append(
                f"take-profit should have fired on {missing} "
                f"(actual BTC calls: {REC.btc_calls})"
            )

    ok = not failures
    return ok, failures or "OK"


def main():
    selected = [a.upper() for a in sys.argv[1:]] or list(SCENARIOS.keys())

    print("=" * 70)
    print("Multi-wheel scenario simulator — regression harness for PR #99")
    print("=" * 70)

    all_pass = True
    for label in selected:
        if label not in SCENARIOS:
            print(f"  unknown scenario: {label}")
            all_pass = False
            continue
        desc, builder = SCENARIOS[label]
        print(f"\n[{label}] {desc}")
        ok, detail = run_scenario(label, desc, builder)
        status = "[PASS]" if ok else "[FAIL]"
        print(f"  {status}")
        if not ok:
            for f in detail:
                print(f"    - {f}")
            all_pass = False
        else:
            print(f"    run_cycle on: {sorted(set(REC.run_cycle_calls))}")
            if REC.btc_calls:
                print(f"    BTC orders:   {REC.btc_calls}")

    print("\n" + "=" * 70)
    if all_pass:
        print("ALL SCENARIOS PASSED — multi-wheel orphan bug stays fixed.")
        sys.exit(0)
    else:
        print("FAILURES DETECTED — do NOT deploy until green.")
        sys.exit(1)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
