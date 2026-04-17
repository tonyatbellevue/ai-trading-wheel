---
name: wheel-recover
description: Recovery helper for the wheel bot when something got stuck — e.g. open/close emails didn't arrive, a scheduled run failed silently, the user sees stale data, the shadow account diverged in a confusing way, or they report "it's broken / emails stopped / the bot seems frozen". Use this skill whenever the user asks to "fix", "recover", "reset", "retry emails", "kick the bot", or "重跑 / 修 / 重置". Runs three things in order: (1) retries any emails sitting in `reports/failed_emails/`, (2) runs `scripts/health_check.py --no-email` so the user sees what's currently passing/failing, (3) optionally resets `metrics/data/shadow_state.json` if the user explicitly asks to rebuild the shadow account from scratch. Prefer over running raw shell commands because it surfaces what was recovered and what still needs attention.
---

# wheel-recover

## What it does
Three-stage recovery:

1. **Retry failed emails** via `utils.emailer.retry_failed_emails()`. These accumulate in `reports/failed_emails/*.json` when SMTP fails — e.g. Gmail rate limit, temporary network error, expired app password. Successful replays are deleted; persistent failures are reported.
2. **Run health check** (`scripts/health_check.py --no-email`). Shows which of the 7 subsystems are green — Alpaca auth, SMTP, disk, failed-email queue, wheel state, shadow rotation.
3. **(Optional) Reset shadow account** if the user explicitly says "reset shadow" or "rebuild shadow". Deletes `metrics/data/shadow_state.json` and the three shadow CSVs. Does NOT reset real account state or trade journal — those are always preserved.

## When to use
- "Emails stopped / I didn't get the open email"
- "Can you retry that thing / replay the failed mail"
- "Shadow account looks wrong, reset it"
- "Run a full health check"
- "Something's broken, figure it out"

## Diagnostic outputs
- Number of emails in queue before / after
- 7-check health report table
- If shadow reset: confirms deletion of state + 3 CSVs

If anything stays red after running (e.g. Alpaca 401 persists), surface the concrete next step from CLAUDE.md "Gotchas" — typically "regenerate the Alpaca key and update both `.env` and GitHub Secrets via `gh secret set ALPACA_API_KEY`".

## Execution

```
cd "C:/Users/belle/OneDrive/Desktop/AI Trading/.claude/worktrees/competent-jones"
python .claude/skills/wheel-recover/assets/recover.py [--reset-shadow]
```

Pass `--reset-shadow` only when the user has explicitly confirmed they want to discard 4 weeks of shadow tracking data.
