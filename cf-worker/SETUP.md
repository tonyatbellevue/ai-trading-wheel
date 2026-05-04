# Wheel Cron Trigger — Cloudflare Worker

A 30-line Worker that fires a GitHub Actions `workflow_dispatch` on
`wheel-summary.yml` at the precise OPEN/CLOSE moments, replacing the
unreliable GHA scheduled cron.

## Why this exists

GHA free-tier scheduled cron is delayed 1-2 hours regularly. Cloudflare
Cron Triggers fire within ~5 seconds of schedule. So:

```
Cloudflare Cron (precise)  →  GitHub workflow_dispatch  →  email_summary.py  →  email
       0 sec                       +30 sec                     +60 sec
```

End-to-end delay drops from 1-2 h to ~2 min.

## Time grid

| Event  | UTC cron        | ET    | SGT          |
| ------ | --------------- | ----- | ------------ |
| OPEN   | `31 13 * * 1-5` | 09:31 | 21:31 same day |
| CLOSE  | `5 20 * * 1-5`  | 16:05 | 04:05 next day |

## One-time setup

### 1. Install Wrangler + deps

```bash
cd cf-worker
npm install
```

### 2. Authenticate to Cloudflare

```bash
npx wrangler login
```

Opens a browser to your Cloudflare account. Free tier is enough.

### 3. Generate a GitHub PAT

Go to https://github.com/settings/personal-access-tokens/new:

- **Resource owner**: `tonyatbellevue`
- **Repository access**: Only select repositories → `ai-trading-wheel`
- **Permissions** → Repository permissions:
  - `Actions` → **Read and write**  ← required
  - (everything else stays "No access")
- **Expiration**: 90 days (rotate when it expires)

Copy the `github_pat_…` token. **Don't commit it.**

### 4. Store the PAT as a Worker secret

```bash
npx wrangler secret put GH_TOKEN
```

Paste the PAT when prompted. Wrangler stores it encrypted in Cloudflare —
it's never written to disk locally and never shows up in the deployed
Worker source.

### 5. Deploy

```bash
npm run deploy
```

This uploads `src/index.ts` + registers the two cron triggers from
`wrangler.toml`. Wrangler prints the worker URL, e.g.
`https://wheel-cron-trigger.<your-subdomain>.workers.dev`.

### 6. Smoke-test

```bash
# Reachability check
curl https://wheel-cron-trigger.<subdomain>.workers.dev/

# Force a real dispatch (sends a forced email — only do this when you
# actually want one)
curl "https://wheel-cron-trigger.<subdomain>.workers.dev/?dispatch=1"
```

Then check https://github.com/tonyatbellevue/ai-trading-wheel/actions —
you should see a new "Wheel Daily Email Summary" run started by
"Triggered via API" within ~30 seconds, and the email arrives shortly
after.

## Daily operation

Nothing — the Worker fires automatically. The CF dashboard at
https://dash.cloudflare.com → Workers & Pages → `wheel-cron-trigger` shows:

- **Cron Events** tab: every cron firing with success/failure
- **Logs** tab: live tail with `npm run tail`

## Recommended follow-up

Once the Worker is verified to fire reliably for ~2 weeks:

1. **Disable** the `schedule:` block in `.github/workflows/wheel-summary.yml`
   (keep `workflow_dispatch:` as the only trigger). Reason: the GHA
   scheduled cron is the source of duplicate emails on different runners
   that can't see each other's `daily_sent_state.json`. With CF as the
   sole trigger, idempotency holds.

2. Keep the GHA `workflow_dispatch:` trigger so manual `gh workflow run`
   still works as a backup if CF ever has an outage.

## Cost

Cloudflare free tier covers:

- Cron Triggers: unlimited on free plan
- Worker invocations: 100k requests/day (we use 2/day)
- Worker CPU: 10ms/request (we use ~3ms — one HTTPS POST)

Total monthly cost: **$0**.

## Troubleshooting

| Symptom                          | Likely cause                                               | Fix                                                                |
| -------------------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------ |
| `dispatch failed: 401`           | PAT expired or wrong scope                                 | Re-issue PAT with `Actions: write`, `wrangler secret put GH_TOKEN` |
| `dispatch failed: 404`           | Wrong owner/repo/workflow file                             | Edit `[vars]` in `wrangler.toml` and `npm run deploy`              |
| `dispatch failed: 422` w/inputs  | Worker sending `inputs` but workflow declares none         | Already handled — but if you add `inputs:` to the YAML, update Worker too |
| Cron fires but no GHA run        | Branch in `GIT_REF` doesn't have `wheel-summary.yml`       | Confirm `master` has the workflow; check CF logs (`npm run tail`)  |
| GHA run fires but no email       | Same idempotency issue or Alpaca/SMTP outage               | Check the GHA run log itself                                       |
