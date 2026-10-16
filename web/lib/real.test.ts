// The form's helpers (#268): the stake looked up in the alert's own table, the checks that mirror
// the database's refusals and the rule's warnings, and the read of the latest alerted day.
import { expect, test } from "vitest";

import { checkPlaced, loadReal, stakeAt } from "./real.ts";
import { ALERT, stubbed } from "./testing.ts";


test("the stake is the table's at the highest listed price at or below the one offered", () => {
  expect(stakeAt(ALERT.stakes, 2.04)).toBe(0);
  expect(stakeAt(ALERT.stakes, 2.05)).toBe(0.6);
  expect(stakeAt(ALERT.stakes, 2.11)).toBe(0.9);
  expect(stakeAt(ALERT.stakes, 3.0)).toBe(1.5);
});

const placed = (changes: Partial<Parameters<typeof checkPlaced>[1]> = {}) => ({
  price: 2.1,
  stake: 0.9,
  betId: "P1",
  placedAt: new Date("2026-11-17T18:00:00Z"),
  ...changes,
});
const NOW = new Date("2026-11-17T18:05:00Z");

test("the form refuses what the database would", () => {
  expect(checkPlaced(ALERT, placed(), NOW)).toEqual({ errors: [], warnings: [] });
  const at = (iso: string) => checkPlaced(ALERT, placed({ placedAt: new Date(iso) }), NOW).errors;
  expect(at("2026-11-18T00:00:00Z")[0]).toMatch(/at or after the game's start/);
  expect(at("2026-11-17T17:00:00Z")[0]).toMatch(/before the alert/);
  expect(at("2026-11-17T18:10:00Z")[0]).toMatch(/in the future/);
  expect(checkPlaced(ALERT, placed({ betId: " " }), NOW).errors).toEqual([
    "Enter the venue's bet id.",
  ]);
  expect(checkPlaced(ALERT, placed({ stake: 0 }), NOW).errors).toHaveLength(1);
});

test("a bet outside the rule is flagged, not refused", () => {
  const low = checkPlaced(ALERT, placed({ price: 2.0, stake: 0.5 }), NOW);
  expect(low.errors).toEqual([]);
  expect(low.warnings[0]).toMatch(/below the minimum 2.05/);
  const over = checkPlaced(ALERT, placed({ price: 2.09, stake: 1.5 }), NOW);
  expect(over.warnings).toEqual(["The rule allows 0.90 € at this price: the stake is above it."]);
});

test("the latest alerted day is read with its entries", async () => {
  const older = { ...ALERT, game_date: "2026-11-16", game_id: 2026020240 };
  const entry = { game_date: ALERT.game_date, game_id: ALERT.game_id, outcome: "not placed" };
  const { supabase, requests } = stubbed(
    { real_alerts: [ALERT, older], real_entries: [entry] },
    new Set(["anon"]),
  );
  const state = await loadReal(supabase);
  expect(state).toEqual({
    ready: true,
    day: "2026-11-17",
    alerts: [ALERT],
    entries: { [ALERT.game_id]: entry },
  });
  const read = requests.find((r) => r.url.pathname.endsWith("/real_entries"));
  expect(read?.url.searchParams.get("game_date")).toBe("eq.2026-11-17");
});

test("before the migration the section says it isn't set up", async () => {
  const { supabase } = stubbed({}, new Set(["anon"]));
  expect(await loadReal(supabase)).toEqual({ ready: false });
});
