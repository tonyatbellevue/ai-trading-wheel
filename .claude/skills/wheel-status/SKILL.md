---
name: wheel-status
description: Show a one-shot account + positions + risk snapshot for the wheel trading bot. Invoke whenever the user asks for account status, current holdings, today's P&L, option positions, buying power, risk exposure, or anything resembling "how's the wheel doing", "check my account", "what do I hold", "当前余额", "持仓", "当日盈亏". Covers equity, cash, BP, each short put (strike / DTE / unrealized P&L / safety cushion), aggregate collateral, and how close we are to the MAX_TOTAL_EXPOSURE_PCT ceiling. The canonical "where do things stand" command for this project.
---

# wheel-status

## What it does
Runs a read-only Python check that produces a compact table: account totals, every short-put position (with OTM/ITM cushion), total collateral, BP utilization, and the remaining risk headroom before hitting the `MAX_TOTAL_EXPOSURE_PCT` ceiling from `config.settings`.

No trades placed, no files modified.

## How to run

From the worktree `C:/Users/belle/OneDrive/Desktop/AI Trading/.claude/worktrees/competent-jones`, execute the inline script `assets/status_report.py` via `python`. It relies on the existing singletons:

- `core.alpaca_client.AlpacaClients.trading()` for `get_account()` and `get_all_positions()`
- `strategy.wheel_strategy._parse_symbol()` to decode OCC option tickers
- `config.settings` for the risk thresholds

## Why this exists
The wheel bot is long-running and largely autonomous. A quick "what's the current state of the world" read is the single most common user ask, and reaching for the full daily email, trade journal CSV, or Alpaca dashboard is friction. This skill is the one-second answer.

## Output shape
A single markdown table block. Ensure numbers are formatted `$1,234.56`. Flag any position that is ITM with ⚠️, any total exposure > 80 % of cap with ⚠️, otherwise ✓. Never lie about delta / current price if the quote endpoint returns stale data — report whatever the API gives and annotate it if the spread is suspicious (e.g. bid = 0).

If `get_all_positions()` throws `unauthorized` that means the Alpaca key rotated — tell the user to refresh `.env` + GitHub Secrets (see CLAUDE.md "Gotchas").

## Execution

```
cd "C:/Users/belle/OneDrive/Desktop/AI Trading/.claude/worktrees/competent-jones"
python .claude/skills/wheel-status/assets/status_report.py
```
