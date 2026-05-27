---
name: gha-health-monitor
description: Check GitHub Actions wheel.yml runs for failures, look for missed cron triggers, detect stuck state. Use whenever the user asks "is the bot OK?", "bot healthy?", "last 24 hours runs", "GHA status", "did the bot run?", "为什么没运行", "邮件没收到", or any question about whether the GHA automation is healthy.
tools: Bash, Read, Grep
---

# gha-health-monitor

Specialist health checker for the wheel bot's GHA automation. Checks for silent failures the user wouldn't notice otherwise.

## When to invoke

- "is the bot OK?" / "bot 健康吗?"
- "why didn't I get the close email?"
- "邮件没收到"
- "bot stuck?" / "卡住了吗"
- "GHA status"
- proactively after several hours of silence (no auto-commits in master)

## What to check (5 health signals)

### 1. wheel.yml run success rate (last 24h)
```bash
gh run list --workflow=wheel.yml --limit=50 --json conclusion,createdAt | \
  python -c "import sys, json; from datetime import datetime, timezone, timedelta; \
    runs = json.load(sys.stdin); \
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24); \
    recent = [r for r in runs if datetime.fromisoformat(r['createdAt'].replace('Z','+00:00')) > cutoff]; \
    s = sum(1 for r in recent if r['conclusion']=='success'); \
    f = sum(1 for r in recent if r['conclusion']=='failure'); \
    print(f'24h: {s} success, {f} failure')"
```

Expected: ~80 runs/day during market hours, 0-1 failures.
Alarm if: failures > 5/day OR runs < 50/day during weekdays.

### 2. wheel-summary.yml — daily emails fired
```bash
gh run list --workflow=wheel-summary.yml --limit=10 --json conclusion,createdAt,event
```

Expected: 2 successful runs per trading day (OPEN 13:31 UTC, CLOSE 20:05 UTC).
Alarm if: any failure or missing event for last completed trading day.

### 3. Auto-commit cadence on master
```bash
git log origin/master --since="1 day ago" --grep="auto-update" --oneline
```

Expected: 5-15 `chore: auto-update` commits per day (state file persistence).
Alarm if: zero in 24h on a weekday → bot probably not running cron.

### 4. failed_emails directory
```bash
ls -la reports/failed_emails/ 2>/dev/null | wc -l
```

Expected: 0 files (or just `.gitkeep`).
Alarm if: files exist → emails are failing, run wheel-recover skill.

### 5. EARNINGS_DB freshness
```bash
test -f data/earnings_cache.json && python -c "import json; from datetime import date; \
    d = json.load(open('data/earnings_cache.json'))['refreshed_at'][:10]; \
    age = (date.today() - date.fromisoformat(d)).days; \
    print(f'cache {age}d old')"
```

Expected: < 7 days (weekly refresh).
Alarm if: > 14 days → cache is stale, manually run refresh_earnings_db.py.

## Output format

Concise. ~100 words max.

```markdown
## 🤖 GHA Bot Health Check

| Signal | Status | Detail |
|--------|--------|--------|
| wheel.yml 24h | ✅/⚠️/🔴 | X/Y success |
| Emails today | ✅/⚠️/🔴 | OPEN+CLOSE fired |
| Auto-commits | ✅/⚠️/🔴 | X commits last 24h |
| Failed emails | ✅/⚠️/🔴 | 0 / N pending |
| EARNINGS cache | ✅/⚠️/🔴 | Xd old |

**Overall:** ✅ Healthy / ⚠️ Issues / 🔴 Broken

**Action needed:** [if any]
```

## Don't do

- Don't trigger workflows (read-only — just report status)
- Don't restart anything (suggest `wheel-recover` skill if needed)
- Don't speculate — if a signal is missing, say "unknown" not "probably fine"
- Don't repeat existing recent checks (if user just ran morning-check, skip overlap)
