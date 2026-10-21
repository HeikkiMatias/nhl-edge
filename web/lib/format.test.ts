import assert from "node:assert/strict";
import { test } from "vitest";

import { easternDate, easternInstant, estimate, helsinkiTime, percent, shortDate } from "./format.ts";

test("an estimate shows its interval, or only its counts with too few weeks", () => {
  assert.equal(
    estimate({ mean: 0.0123, low: 0.0045, high: 0.02, games: 120, weeks: 9 }, "bets"),
    "+0.0123 [+0.0045, +0.0200] (120 bets, 9 weeks)",
  );
  assert.equal(
    estimate({ games: 12, weeks: 2 }, "bets"),
    "12 bets over 2 weeks: too few weeks for an estimate",
  );
  assert.equal(estimate({ value: -0.01, low: -0.02, high: 0.0 }), "-0.0100 [-0.0200, +0.0000]");
  assert.equal(estimate(null), "none yet");
});

test("game days are US Eastern, as the ledger keys games", () => {
  // 01:30 UTC on 2026-10-09 is 21:30 on 2026-10-08 in New York.
  assert.equal(easternDate(new Date("2026-10-09T01:30:00Z")), "2026-10-08");
  assert.equal(percent(0.5234), "52.3%");
  assert.equal(percent(null), "");
});

test("clock times are Helsinki time, with the weekday when it's past the game day", () => {
  // A 19:00 ET start on Thursday 2026-10-08 is 02:00 on Friday in Helsinki.
  assert.equal(helsinkiTime("2026-10-08T23:00:00Z", "2026-10-08"), "Fri 02:00");
  assert.equal(helsinkiTime("2026-10-08T16:45:00Z", "2026-10-08"), "19:45");
  assert.equal(helsinkiTime("2026-10-09T10:28:00Z"), "13:28");
});

test("the 12:45 ET decision is 19:45 in Helsinki, or 18:45 while only one has changed clocks", () => {
  const decision = (day: string) => helsinkiTime(easternInstant(day, "12:45").toISOString());
  assert.equal(decision("2026-10-09"), "19:45"); // both on summer time
  assert.equal(decision("2026-10-26"), "18:45"); // Helsinki back on winter time on 10-25, New York not
  assert.equal(decision("2026-11-02"), "19:45"); // both on winter time
  assert.equal(decision("2027-03-15"), "18:45"); // New York on summer time from 03-14, Helsinki not
  assert.equal(decision("2027-03-29"), "19:45"); // both on summer time from 03-28
  assert.equal(easternInstant("2026-10-09", "12:45").toISOString(), "2026-10-09T16:45:00.000Z");
});

test("a chart's date label is the short month and day", () => {
  assert.equal(shortDate("2026-10-08"), "Oct 8");
  assert.equal(shortDate("2027-01-03"), "Jan 3");
});
