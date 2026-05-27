---
name: morning-check
description: One-shot morning briefing for the wheel bot — combines account status, last 24h bot health, today's earnings calendar, and market pre-open context. Use whenever the user says "morning check", "早上好", "早报", "今天怎么样", or asks for a daily snapshot before market open. Saves 5 manual queries (wheel-status + gh run list + EARNINGS_DB lookup + SPY/QQQ check + sector ETF check) into one combined view. Designed for the daily SGT morning routine (08:00-09:00 SGT before US market open).
---

# morning-check

## What it does

Runs a comprehensive morning briefing in one command. Replaces 5 separate manual queries:

1. **Account status** — equity, cash, positions, today P&L (delegates to wheel-status logic)
2. **Bot health (last 24h)** — recent GHA wheel.yml runs, any failures
3. **Today's earnings** — which wheel-universe symbols report today/tomorrow
4. **Market context** — SPY/QQQ/VIX overnight + sector ETF performance
5. **Wheel symbol focus** — current active symbol's 3-day chart + IV trend

## When to invoke

Daily morning routine. Trigger phrases:
- "morning check"
- "早上好" / "早报" / "今天怎么样"
- "give me the morning brief"
- "how's everything"

Especially useful at SGT 08:00-09:00 (US market opens 21:30 SGT, so this is your pre-game prep).

## Execution

```
cd "C:/Users/belle/OneDrive/Desktop/AI Trading/.claude/worktrees/competent-jones"
python .claude/skills/morning-check/assets/brief.py
```

## Output structure

```markdown
# 🌅 Morning Brief — <date> <time SGT>

## 💰 Account
| Equity | Cash | Today P&L |
| $X | $Y | +/-$Z |

## 📊 Positions
[wheel-status compact table]

## 🤖 Bot health (last 24h)
✅ 12/12 GHA runs success
or
⚠️ 3 failures detected — last: 2026-05-27 13:35 UTC

## 📅 Today's earnings (wheel universe)
- CRM 5/27 AMC (cash-only)
- ...

## 🌍 Market pre-open
- SPY futures: +0.3%
- VIX: 15.2 (calm)
- SMH: +0.5% (semis)

## 🎯 Action items
[any explicit suggestions based on today's data]
```

## Why this exists

The user's daily routine at SGT 08:00 was:
1. Open Alpaca app → check balance
2. Open GitHub → check Actions tab for failed runs
3. Open email → read close brief
4. Check earnings calendar manually
5. Look at SPY/QQQ pre-market

5 apps × 5 minutes = 25 min/day of friction. This skill makes it 1 command, 30 seconds.

## Don't do

- Don't place any trades (read-only briefing)
- Don't email — Claude renders the brief inline
- Don't analyze with opinion unless data clearly warrants (e.g., bot failures)
- Don't include data that's already in close email (avoid duplication)
