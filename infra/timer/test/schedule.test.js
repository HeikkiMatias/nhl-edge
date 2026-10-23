import assert from "node:assert/strict";
import { test } from "node:test";

import { KALSHI_REVALUE_ET, dueJobs, easternClock } from "../src/schedule.js";

const at = (iso) => Date.parse(iso);
const names = (iso) => dueJobs(at(iso)).map((j) => `${j.workflow}${j.inputs.slot ? `:${j.inputs.slot}` : ""}`);

test("odds slots follow US Eastern time in EDT and EST", () => {
  // 07:05 ET also starts the Kalshi watch's first window (#319).
  assert.deepEqual(names("2026-09-29T11:05:00Z"), ["odds-snapshots.yml:morning", "kalshi-watch.yml"]);
  assert.deepEqual(names("2026-12-01T12:05:00Z"), ["odds-snapshots.yml:morning", "kalshi-watch.yml"]);
  assert.deepEqual(names("2026-09-29T22:45:00Z"), ["odds-snapshots.yml:pre7"]);
  assert.deepEqual(names("2026-12-01T23:45:00Z"), ["odds-snapshots.yml:pre7"]);
  assert.deepEqual(names("2026-09-30T01:45:00Z"), ["odds-snapshots.yml:pre10"]);
  assert.deepEqual(names("2026-12-02T02:45:00Z"), ["odds-snapshots.yml:pre10"]);
  assert.deepEqual(names("2026-12-01T11:05:00Z"), []);
});

test("goalie polls at :50 from 12:50 to 02:50 UTC, and the nightly ingest at 09:00 UTC", () => {
  assert.deepEqual(names("2026-09-29T12:50:00Z"), ["pregame-goalies.yml"]);
  assert.deepEqual(names("2026-09-30T02:50:00Z"), ["kalshi-revalue.yml", "pregame-goalies.yml"]);
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

test("the Kalshi watch starts at its four US Eastern windows in EDT and EST (#319)", () => {
  // 07:05 ET is also the morning odds slot.
  assert.deepEqual(names("2026-10-23T11:05:00Z"), ["odds-snapshots.yml:morning", "kalshi-watch.yml"]);
  assert.deepEqual(names("2026-12-01T12:05:00Z"), ["odds-snapshots.yml:morning", "kalshi-watch.yml"]);
  assert.deepEqual(names("2026-10-23T15:05:00Z"), ["kalshi-watch.yml"]);
  assert.deepEqual(names("2026-10-23T19:05:00Z"), ["kalshi-watch.yml", "kalshi-revalue.yml"]);
  assert.deepEqual(names("2026-10-23T23:05:00Z"), ["kalshi-watch.yml", "kalshi-revalue.yml"]);
  assert.deepEqual(names("2026-12-02T00:05:00Z"), ["kalshi-watch.yml", "kalshi-revalue.yml"]);
  assert.deepEqual(names("2026-10-23T15:10:00Z"), []);
});

test("the Kalshi re-valuation every 15 minutes from 13:05 to 22:50 US Eastern, clear of the slots", () => {
  // 13:05 EDT is 17:05 UTC; 13:05 EST is 18:05 UTC.
  assert.deepEqual(names("2026-10-23T17:05:00Z"), ["kalshi-revalue.yml"]);
  assert.deepEqual(names("2026-12-01T18:05:00Z"), ["kalshi-revalue.yml"]);
  assert.deepEqual(names("2026-10-23T17:20:00Z"), ["kalshi-revalue.yml"]);
  assert.deepEqual(names("2026-10-24T02:35:00Z"), ["kalshi-revalue.yml"]);
  // before the decision's orders, after 22:50, and at the slots' :45
  assert.deepEqual(names("2026-10-23T16:50:00Z"), ["pregame-goalies.yml"]);
  assert.deepEqual(names("2026-10-24T03:05:00Z"), []);
  assert.deepEqual(names("2026-10-23T22:45:00Z"), ["odds-snapshots.yml:pre7"]);
  assert.equal(KALSHI_REVALUE_ET.length, 40);
  assert.equal(KALSHI_REVALUE_ET.at(-1), "22:50");
});
