# nhl-edge-timer

A Cloudflare Worker that starts the repo's scheduled workflows on time (issue #50). GitHub's own
`schedule` started runs 3 to 6 hours late, while a `workflow_dispatch` starts within seconds. The
Worker's cron fires every five minutes, and `src/schedule.js` decides which workflows are due:

| Workflow | When | Inputs |
| --- | --- | --- |
| `odds-snapshots.yml` | 07:05, 12:45, 18:45, 19:45 and 21:45 US Eastern (DST included) | `slot`: morning, midday, pre7, pre8, pre10 |
| `pregame-goalies.yml` | :50 of every hour from 12:50 to 02:50 UTC | none |
| `ingest-nightly.yml` | 09:00 UTC | none |

Each is dispatched on `main` through the GitHub API. A failed dispatch is retried on 429 and 5xx,
logged, and makes the cron event show as failed in the Cloudflare dashboard. It runs on the Workers
free plan: one cron trigger and 288 short invocations a day.

The workflows keep their GitHub cron lines as a fallback. While the repository variable
`TIMER_ACTIVE` is `true`, a scheduled run skips its job, so each job runs once, on time. Until the
Worker is deployed, or with the variable unset or `false`, the late GitHub schedule runs as before.

## Setup

1. **GitHub token.** GitHub, Settings, Developer settings, Fine-grained personal access tokens,
   Generate new token. Repository access: only `HeikkiMatias/nhl-edge`. Permissions: Actions, read
   and write (Metadata read-only is added by GitHub). Pick an expiry and note the date: when the
   token expires, every dispatch fails with 401.
2. **Deploy**, from this folder, with the Cloudflare account that holds the R2 bucket:

   ```sh
   npx wrangler@4 login
   npx wrangler@4 deploy
   npx wrangler@4 secret put GITHUB_TOKEN   # paste the token
   ```

3. **Check** at the next due time, such as the next :50 between 12:50 and 02:50 UTC: the Actions tab
   shows a `pregame-goalies` run with the event `workflow_dispatch`, and `npx wrangler@4 tail`
   prints `dispatched pregame-goalies.yml {}`.
4. **Switch off the late schedule:** GitHub, the repo's Settings, Secrets and variables, Actions,
   Variables tab, New repository variable `TIMER_ACTIVE` with the value `true`.

To renew the token, run `npx wrangler@4 secret put GITHUB_TOKEN` again. If the Worker stops, set
`TIMER_ACTIVE` to `false` and the GitHub schedule takes over, late. To change a time, edit
`src/schedule.js`, run `npm test`, and deploy.

## Tests

`npm test` (or `node --test` in this folder) runs the tests in `test/` with no dependencies. CI runs
them too. `npx wrangler@4 dev --test-scheduled` runs the Worker locally; a cron event at a chosen
time is fired with `curl "http://localhost:8787/cdn-cgi/handler/scheduled?cron=*/5+*+*+*+*&time=<epoch ms>"`.
Local runs dispatch for real with whatever `GITHUB_TOKEN` they are given.
