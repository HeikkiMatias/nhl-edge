import assert from "node:assert/strict";
import { test } from "vitest";

import { easternDate, easternTime, estimate, percent, shortDate } from "./format.ts";

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

test("dates and times are US Eastern, as the ledger keys games", () => {
  // 01:30 UTC on 2026-10-09 is 21:30 on 2026-10-08 in New York.
  assert.equal(easternDate(new Date("2026-10-09T01:30:00Z")), "2026-10-08");
  assert.equal(easternTime("2026-10-08T23:00:00Z"), "19:00");
  assert.equal(percent(0.5234), "52.3%");
  assert.equal(percent(null), "");
});

test("a chart's date label is the short month and day", () => {
  assert.equal(shortDate("2026-10-08"), "Oct 8");
  assert.equal(shortDate("2027-01-03"), "Jan 3");
});
