---
name: pre-trade-check
description: Comprehensive pre-trade safety scan for ANY option trade — runs all 8 bot filters PLUS extra real-money checks. Use whenever the user is about to manually enter a real-broker option trade (especially Sell Put) on Merrill Lynch / TastyTrade / other non-Alpaca account. Triggers on phrases like "I'm going to sell a put on X", "want to do Y trade", "下一笔交易", "买回 / 实盘 / 真账户". Catches earnings traps (CRM 5/26 incident), sector-wide selloffs, hidden IV crush risk, and concentration with existing positions. Prevents the manual-trade gap where bot filters protect Alpaca but real money goes unchecked.
---

# pre-trade-check

## What it does

Runs an 8-point safety scan on a candidate option trade BEFORE you hit submit on your real broker. Same filters the bot uses on Alpaca paper, plus extra context (earnings calendar, news sentiment, existing position concentration).

**Read-only.** No trades executed. Pure analysis + recommendation.

## When to invoke

Triggered by user phrases like:
- "I'm thinking of selling X put"
- "下一笔做 XXX 怎么样"
- "实盘做 SYMBOL"
- "Should I sell XYZ"
- "/pre-trade-check SYMBOL STRIKE EXPIRY"

Also after the user shares a screenshot of an unsubmitted broker order.

## Inputs needed

| Required | Optional |
|----------|----------|
| Symbol | Strike price |
| Expiry date | Premium (mid/limit) |

If user only gives symbol, suggest 1-2 strikes from current option chain.

## 8-point check (mirrors bot's pre_open_put_checks + extras)

1. **Earnings within 7 days** — Finnhub cache + Yahoo verify (CRM 5/27 must trigger ❌)
2. **IV rank** — must be ≥ 30 (else premium thin)
3. **VIX regime** — < 25 (calm enough to sell premium)
4. **MA50 trend** — symbol above its 50-day MA
5. **Realized vol cap** — daily RV < 90% annualized
6. **Recent drop** — past 3 days cumulative > -5%
7. **Sector ETF** — sector ETF today > -2%
8. **Annualized premium** — (premium/strike)×(365/dte) ≥ 20%

Plus 3 real-money-specific checks:
- **OTM distance** — must be ≥ 3% for safety
- **Concentration with existing positions** — sum of similar-sector exposures < 60%
- **Liquidity** — bid-ask spread < 10%, OI > 100, volume > 50

## Execution

```bash
cd "C:/Users/belle/OneDrive/Desktop/AI Trading/.claude/worktrees/competent-jones"
python .claude/skills/pre-trade-check/assets/check.py <SYMBOL> [STRIKE] [EXPIRY]
```

Script returns a markdown report with 🟢/🟡/🔴 per check.

## Output template

```markdown
# Pre-Trade Check: <SYMBOL> <STRIKE>P expiring <EXPIRY>

## ✅ Passes
- Earnings T+XXd ✓
- IV rank XX ✓
- ...

## 🟡 Warnings
- ...

## 🔴 Blockers
- ...

## Recommendation
[GO / CAUTION / STOP] + 1-sentence why
```

## Why this exists

5/26 CRM incident: user manually sold CRM 5/29 $165P on Merrill in real account, then we discovered CRM had earnings 5/27 (1 day later). Bot's `EARNINGS_DB` correctly contained CRM date, but the manual order bypassed all bot filters because it was on Merrill not Alpaca.

This skill IS the bot's safety net for manual trades. Use it religiously before real-money option orders.

## Don't do

- Don't fetch live broker positions — this skill is for vetting a candidate, not auditing existing positions (use `wheel-status` for that)
- Don't auto-submit trades — analysis only
- Don't lie about earnings — if Finnhub returns nothing, say "unknown" not "safe"
- Don't sugar-coat — if it's a bad trade, say STOP clearly
