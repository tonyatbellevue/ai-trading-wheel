/**
 * Cloudflare Worker — wheel email cron trigger.
 *
 * Why this exists: GitHub Actions free-tier scheduled cron is regularly
 * delayed 1-2 hours, so the wheel OPEN/CLOSE summary emails arrive late
 * (or, when GHA drops the run entirely, never).
 *
 * Cloudflare Cron Triggers fire within ~5 seconds of schedule. This Worker
 * fires at the exact OPEN/CLOSE moments and POSTs to the GitHub Actions
 * workflow_dispatch API, which boots the existing wheel-summary.yml workflow
 * within ~30 seconds. Total perceived delay: ~2 minutes vs current 1-2 hours.
 *
 * The dispatched run sets WHEEL_EMAIL_FORCE=1, bypassing the local-file
 * idempotency in email_summary.py — so a duplicate scheduled GHA run that
 * later catches up will be skipped by daily_sent_state.json.
 */

interface Env {
  GH_OWNER: string;
  GH_REPO: string;
  GH_TOKEN: string;        // PAT with `actions:write`. Set via `wrangler secret put GH_TOKEN`.
  WORKFLOW_FILE: string;   // e.g. "wheel-summary.yml"
  GIT_REF: string;         // branch to dispatch on, e.g. "master"
}

async function dispatchWorkflow(env: Env, label: string): Promise<void> {
  const url = `https://api.github.com/repos/${env.GH_OWNER}/${env.GH_REPO}/actions/workflows/${env.WORKFLOW_FILE}/dispatches`;
  // wheel-summary.yml's workflow_dispatch declares no inputs, so we must
  // POST with `ref` only — extra `inputs` keys would 422.
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GH_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "wheel-cron-trigger/1.0",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: env.GIT_REF }),
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`GitHub dispatch failed [${label}]: ${res.status} ${res.statusText} — ${body}`);
  }
}

export default {
  // Cron Trigger entry point. Fired by Cloudflare's scheduler per the
  // crons[] list in wrangler.toml.
  async scheduled(event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    const fired = new Date(event.scheduledTime).toISOString();
    const label = `cron="${event.cron}" scheduledTime=${fired}`;
    console.log(`[wheel-cron] firing dispatch — ${label}`);
    ctx.waitUntil(
      dispatchWorkflow(env, label).then(
        () => console.log(`[wheel-cron] dispatch ok — ${label}`),
        (err: Error) => console.error(`[wheel-cron] ${err.message}`)
      )
    );
  },

  // Manual smoke-test endpoint. `curl https://<worker>.workers.dev/`
  // returns 200 if the Worker is reachable. `?dispatch=1` will actually
  // trigger a dispatch — handy for verifying the GH PAT works without
  // waiting for the next cron tick.
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.searchParams.get("dispatch") === "1") {
      try {
        await dispatchWorkflow(env, "manual");
        return new Response("dispatch ok\n", { status: 200 });
      } catch (err) {
        return new Response(`dispatch failed: ${(err as Error).message}\n`, { status: 500 });
      }
    }
    return new Response(
      `wheel-cron-trigger alive. Crons configured in wrangler.toml.\n` +
      `Hit /?dispatch=1 to manually fire a workflow_dispatch.\n`,
      { status: 200, headers: { "Content-Type": "text/plain" } }
    );
  },
};
