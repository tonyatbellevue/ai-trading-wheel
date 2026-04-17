---
name: wheel-switch
description: Register a planned rotation of the wheel bot's active symbol. Use this whenever the user says things like "switch to AAPL", "change wheel to MSFT", "rotate to NVDA", "plan a move to AVGO", or anything that implies "after the current cycle closes, start selling puts on a different underlying". The skill validates the target is in `wheel_scanner.WHEEL_UNIVERSE`, picks a safe trigger date (next Friday expiry + 1 so we don't interrupt an active put), and writes to `wheel_symbol.json`. Does NOT force an immediate switch — the swap only fires on the next `run_cycle` if the bot is IDLE and today ≥ trigger_date. For emergency override use `python wheel_switch_cli.py force TICKER` directly.
---

# wheel-switch

## What it does
Registers a symbol-rotation plan. Writes to `wheel_symbol.json` via `strategy.wheel_switch.plan_switch()`. The live `run_cycle()` picks up the plan automatically on the next cron tick and acts on it **only when** the bot is IDLE and today ≥ `trigger_date`.

## Inputs
- **to_symbol** (required) — e.g. `AAPL`, `AVGO`. Case insensitive.
- **reason** (optional) — free text for the plan record, e.g. "shadow v3 scored higher".

## Validation
1. Reject if `to_symbol.upper()` not in `wheel_scanner.WHEEL_UNIVERSE`. List the accepted universe in the error so the user can pick again.
2. Reject if `to_symbol` equals the currently-active symbol (already there, nothing to do).
3. Default `trigger_date` = next Saturday (next Friday expiry + 1 so any open put on the current symbol expires first). `strategy.wheel_evaluator._next_expiry_friday_plus_one()` computes this.

## Output
Print the plan summary plus the existing plan state after the write. Remind the user that:
- The swap is latent — the bot only fires it when IDLE.
- They can cancel with `python wheel_switch_cli.py cancel`.
- Force-now escape hatch: `python wheel_switch_cli.py force TICKER` (bypasses IDLE check; dangerous if positions are open).

## Why a plan instead of an instant write
Rotating mid-cycle would orphan the put we just sold — we'd still be on the hook for assignment on the OLD symbol while the bot thinks the NEW symbol is active. Waiting until IDLE (Saturday after Friday expiry) is the natural breakpoint.

## Execution

```
cd "C:/Users/belle/OneDrive/Desktop/AI Trading/.claude/worktrees/competent-jones"
python .claude/skills/wheel-switch/assets/plan.py <TICKER> [reason...]
```
