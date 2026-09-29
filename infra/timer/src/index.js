// Cloudflare Worker that starts the repo's scheduled workflows on time (issue #50). See README.md.
import { dueJobs } from "./schedule.js";

const API = "https://api.github.com";
const ATTEMPTS = 3;

/**
 * Dispatch one workflow on env.GITHUB_REF. Retries on 429 and 5xx; throws when it cannot.
 * `fetchImpl` and `sleep` are replaced in tests.
 */
export async function dispatch(env, job, fetchImpl = fetch, sleep = defaultSleep) {
  const url = `${API}/repos/${env.GITHUB_REPO}/actions/workflows/${job.workflow}/dispatches`;
  const init = {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent": "nhl-edge-timer",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({ ref: env.GITHUB_REF, inputs: job.inputs }),
  };
  let last = "";
  for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
    const res = await fetchImpl(url, init);
    if (res.ok) {
      console.log(`dispatched ${job.workflow} ${JSON.stringify(job.inputs)}`);
      return;
    }
    last = `${res.status} ${(await res.text()).slice(0, 300)}`;
    if (res.status !== 429 && res.status < 500) break;
    if (attempt < ATTEMPTS) await sleep(attempt * 5_000);
  }
  throw new Error(`dispatch of ${job.workflow} failed: ${last}`);
}

function defaultSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export default {
  async scheduled(controller, env) {
    const jobs = dueJobs(controller.scheduledTime);
    const results = await Promise.allSettled(jobs.map((job) => dispatch(env, job)));
    const errors = results.filter((r) => r.status === "rejected").map((r) => r.reason);
    for (const error of errors) console.error(String(error));
    // A thrown error marks the cron event as failed in the Cloudflare dashboard.
    if (errors.length) throw new AggregateError(errors, `${errors.length} of ${jobs.length} dispatches failed`);
  },
};
