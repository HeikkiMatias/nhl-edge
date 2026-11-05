// The Models tab's figures (#398 step 5): each challenger's primary measure, the points a chart
// may draw (with intervals only, hard rule 7), and the dates and progress the cards show.
// Synthetic figures only.
import { expect, test } from "vitest";

import {
  type ChallengerEntry,
  elapsed,
  fitProgress,
  hasInterval,
  intervalAbout,
  type ModelNight,
  primaryFigure,
  primaryName,
  primaryOf,
  series,
} from "./models.ts";

const entry = (changes: Partial<ChallengerEntry> = {}): ChallengerEntry => ({
  name: "conservative_blend",
  version: "1",
  adr: "0041",
  registered_on: "2026-10-22",
  days: 10,
  games: 66,
  predicted: 66,
  not_predicted: {},
  missing_days: [],
  pending_days: [],
  ...changes,
});
const counts = { games: 66, weeks: 2 };
const interval = { mean: -0.004, low: -0.01, high: 0.002, games: 300, weeks: 5 };

test("a challenger's primary measure is the report's own, or its ADR's rule in older reports", () => {
  expect(primaryOf(entry({ primary: "B3" }))).toBe("B3");
  expect(primaryOf(entry({ primary: null }))).toBeNull();
  // Before the field: a close forecaster's move, a baseline, the control's none, else the blend.
  expect(primaryOf(entry({ forecasts: "close" }))).toBe("move");
  expect(primaryOf(entry({ baseline: "conservative_blend/1" }))).toBe("conservative_blend/1");
  expect(primaryOf(entry({ name: "market_only" }))).toBeNull();
  expect(primaryOf(entry())).toBe("BLEND");
});

test("its figure is its paired difference, or its move, and none for the control", () => {
  const comparisons = { BLEND: { difference: interval, left_out: 0 }, B1: { difference: counts, left_out: 0 } };
  expect(primaryFigure(entry({ comparisons }))).toEqual(interval);
  expect(primaryFigure(entry({ primary: "move", move: { difference: counts } }))).toEqual(counts);
  expect(primaryFigure(entry({ primary: null, comparisons }))).toBeNull();
  expect(primaryFigure(undefined)).toBeNull();
  expect(primaryName(entry())).toBe("Log loss against the frozen blend, the challenger's less (below 0 favours it)");
  expect(primaryName(entry({ primary: null }))).toMatch(/^The control/);
  // The close challenger's figure is its squared error less no move's (Codex on #406).
  expect(primaryName(entry({ primary: "move" }))).toBe(
    "Its forecast's squared error on the move to Pinnacle's close less no move's (below 0 favours it)",
  );
});

test("a chart's points are only the nights whose figure has its interval", () => {
  const nights = [
    { as_of: "2026-11-01", slope: counts },
    { as_of: "2026-11-20", slope: { value: 0.9, low: 0.7, high: 1.1, games: 400, weeks: 4 } },
    { as_of: "2026-11-21", slope: null },
  ] as unknown as ModelNight[];
  expect(series(nights, (n) => n.slope)).toEqual([{ as_of: "2026-11-20", value: 0.9, low: 0.7, high: 1.1 }]);
  expect(hasInterval(counts)).toBe(false);
  expect(hasInterval(interval)).toBe(true);
});

test("the cards say about when an interval comes, the training games and the way to the review", () => {
  // Two weeks of games on 11-04: two more to go.
  expect(intervalAbout(counts, "2026-11-04")).toBe("2026-11-18");
  expect(intervalAbout(null, "2026-11-04")).toBe("2026-12-02");
  expect(intervalAbout(interval, "2026-11-04")).toBeNull();
  expect(fitProgress(entry({ latest_fit: { games: 77, minimum: 200, week: "2026-10-26" } }))).toEqual({
    games: 77,
    minimum: 200,
    share: 0.385,
  });
  expect(fitProgress(entry({ latest_fit: { games: 250, minimum: 200 } }))?.share).toBe(1);
  expect(fitProgress(entry())).toBeNull();
  expect(elapsed("2026-10-22", "2027-04-12", "2026-10-22")).toBe(0);
  expect(elapsed("2026-10-22", "2027-04-12", "2027-05-01")).toBe(1);
  expect(elapsed("2026-10-22", "2027-04-12", "2027-01-16")).toBeCloseTo(0.5, 1);
});
