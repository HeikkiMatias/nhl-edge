import assert from "node:assert/strict";
import { test } from "node:test";

import worker, { dispatch } from "../src/index.js";

const env = { GITHUB_REPO: "owner/repo", GITHUB_REF: "main", GITHUB_TOKEN: "t" };
const job = { workflow: "odds-snapshots.yml", inputs: { slot: "pre7" } };
const noSleep = async () => {};

function fakeFetch(statuses) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    const status = statuses[Math.min(calls.length - 1, statuses.length - 1)];
    return new Response(status === 204 ? null : "error", { status });
  };
  fn.calls = calls;
  return fn;
}

test("posts the workflow, ref and inputs to the dispatch endpoint", async () => {
  const fetch = fakeFetch([204]);
  await dispatch(env, job, fetch, noSleep);
  assert.equal(fetch.calls.length, 1);
  const { url, init } = fetch.calls[0];
  assert.equal(url, "https://api.github.com/repos/owner/repo/actions/workflows/odds-snapshots.yml/dispatches");
  assert.equal(init.method, "POST");
  assert.equal(init.headers.Authorization, "Bearer t");
  assert.deepEqual(JSON.parse(init.body), { ref: "main", inputs: { slot: "pre7" } });
});

test("retries a server error, and gives up after three attempts", async () => {
  const recovers = fakeFetch([502, 204]);
  await dispatch(env, job, recovers, noSleep);
  assert.equal(recovers.calls.length, 2);

  const down = fakeFetch([503]);
  await assert.rejects(dispatch(env, job, down, noSleep), /odds-snapshots.yml failed: 503/);
  assert.equal(down.calls.length, 3);
});

test("does not retry a client error such as a bad token", async () => {
  const fetch = fakeFetch([401]);
  await assert.rejects(dispatch(env, job, fetch, noSleep), /failed: 401/);
  assert.equal(fetch.calls.length, 1);
});

test("the cron handler throws when a dispatch fails, so the event shows as failed", async () => {
  const saved = globalThis.fetch;
  globalThis.fetch = fakeFetch([401]);
  const quiet = console.error;
  console.error = () => {};
  try {
    // 22:45 UTC on 2026-09-29 is the pre7 slot.
    await assert.rejects(worker.scheduled({ scheduledTime: Date.parse("2026-09-29T22:45:00Z") }, env), /1 of 1/);
    // A fire with nothing due makes no request.
    globalThis.fetch = fakeFetch([204]);
    await worker.scheduled({ scheduledTime: Date.parse("2026-09-29T22:40:00Z") }, env);
    assert.equal(globalThis.fetch.calls.length, 0);
  } finally {
    globalThis.fetch = saved;
    console.error = quiet;
  }
});
