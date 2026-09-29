import assert from "node:assert/strict";
import { test } from "node:test";

import { dueJobs, easternClock } from "../src/schedule.js";

const at = (iso) => Date.parse(iso);
const names = (iso) => dueJobs(at(iso)).map((j) => `${j.workflow}${j.inputs.slot ? `:${j.inputs.slot}` : ""}`);

test("odds slots follow US Eastern time in EDT and EST", () => {
  assert.deepEqual(names("2026-09-29T11:05:00Z"), ["odds-snapshots.yml:morning"]);
  assert.deepEqual(names("2026-12-01T12:05:00Z"), ["odds-snapshots.yml:morning"]);
  assert.deepEqual(names("2026-09-29T22:45:00Z"), ["odds-snapshots.yml:pre7"]);
  assert.deepEqual(names("2026-12-01T23:45:00Z"), ["odds-snapshots.yml:pre7"]);
  assert.deepEqual(names("2026-09-30T01:45:00Z"), ["odds-snapshots.yml:pre10"]);
  assert.deepEqual(names("2026-12-02T02:45:00Z"), ["odds-snapshots.yml:pre10"]);
  assert.deepEqual(names("2026-12-01T11:05:00Z"), []);
});

test("goalie polls at :50 from 12:50 to 02:50 UTC, and the nightly ingest at 09:00 UTC", () => {
  assert.deepEqual(names("2026-09-29T12:50:00Z"), ["pregame-goalies.yml"]);
  assert.deepEqual(names("2026-09-30T02:50:00Z"), ["pregame-goalies.yml"]);
  assert.deepEqual(names("2026-09-30T03:50:00Z"), []);
  assert.deepEqual(names("2026-09-30T11:50:00Z"), []);
  assert.deepEqual(names("2026-09-30T09:00:00Z"), ["ingest-nightly.yml"]);
});

test("seconds past the minute are dropped", () => {
  assert.deepEqual(names("2026-09-29T22:45:59Z"), ["odds-snapshots.yml:pre7"]);
  assert.deepEqual(names("2026-09-29T22:46:00Z"), []);
});

test("every five-minute fire of a year gives each job exactly once a day", () => {
  const start = at("2026-01-01T00:00:00Z");
  const end = at("2027-01-01T00:00:00Z");
  const counts = new Map();
  for (let ms = start; ms < end; ms += 5 * 60_000) {
    for (const job of dueJobs(ms)) {
      const key = job.inputs.slot ?? job.workflow;
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
  }
  // 2026 has 365 days, and both DST changes happen at 02:00 ET, away from every slot.
  for (const slot of ["morning", "midday", "pre7", "pre8", "pre10"]) assert.equal(counts.get(slot), 365, slot);
  assert.equal(counts.get("pregame-goalies.yml"), 365 * 15);
  assert.equal(counts.get("ingest-nightly.yml"), 365);
});

test("the Eastern clock uses 00 to 23", () => {
  assert.equal(easternClock(at("2026-09-30T04:05:00Z")), "00:05");
  assert.equal(easternClock(at("2026-09-29T23:45:00Z")), "19:45");
});
