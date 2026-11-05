// The form's helpers (#268): the stake looked up in the alert's own table, the checks that mirror
// the database's refusals and the rule's warnings, and the read of the latest alerted day.
import { expect, test } from "vitest";

import { checkNote, checkPlaced, loadReal, NOTE_MAX, stakeAt, standing } from "./real.ts";
import { ALERT, stubbed } from "./testing.ts";
import type { RealEntry, RealRevision } from "./types.ts";


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
    // Before the revisions migration: none, and no correcting from the form (ADR 0058).
    revisions: null,
    corrections: {},
  });
  const read = requests.find((r) => r.url.pathname.endsWith("/real_entries"));
  expect(read?.url.searchParams.get("game_date")).toBe("eq.2026-11-17");
});

test("before the migration the section says it isn't set up", async () => {
  const { supabase } = stubbed({}, new Set(["anon"]));
  expect(await loadReal(supabase)).toEqual({ ready: false });
});

test("before the Kalshi migration the read leaves its columns out", async () => {
  // Postgres answers "undefined column" for exchange and source until the migration is applied
  // (#287): the read asks again without them.
  const undefinedColumn = (sent: { url: URL; method: string }) => {
    const columns = sent.url.searchParams.get("select") ?? "";
    if (sent.method === "GET" && (columns.includes("exchange") || columns.includes("source"))) {
      return { status: 400, body: { code: "42703", message: "column does not exist" } };
    }
    return undefined;
  };
  const entry = { game_date: ALERT.game_date, game_id: ALERT.game_id, outcome: "not placed" };
  const { supabase, requests } = stubbed(
    { real_alerts: [ALERT], real_entries: [entry] },
    new Set(["anon"]),
    undefinedColumn,
  );
  const state = await loadReal(supabase);
  expect(state).toEqual({
    ready: true,
    day: "2026-11-17",
    alerts: [ALERT],
    entries: { [ALERT.game_id]: entry },
    // Before the revisions migration: none, and no correcting from the form (ADR 0058).
    revisions: null,
    corrections: {},
  });
  const selects = requests.map((r) => r.url.searchParams.get("select") ?? "");
  expect(selects.filter((s) => s.includes("exchange"))).toHaveLength(1);
  expect(selects.filter((s) => s.includes("source"))).toHaveLength(1);
});

const ENTRY: RealEntry = {
  game_date: ALERT.game_date,
  game_id: ALERT.game_id,
  outcome: "placed",
  matched_price: 2.1,
  stake: 0.9,
  bet_id: "P1",
  placed_utc: "2026-11-17T18:00:00Z",
  reason: null,
  entered_utc: "2026-11-17T18:01:00Z",
  late: false,
  source: "form",
};
const revision = (n: number, changes: Partial<RealRevision>): RealRevision => ({
  game_date: ALERT.game_date,
  game_id: ALERT.game_id,
  n,
  outcome: "placed",
  matched_price: null,
  stake: null,
  bet_id: null,
  placed_utc: null,
  reason: null,
  note: "why",
  revised_utc: `2026-11-17T18:0${n + 1}:30Z`,
  ...changes,
});

test("the day's revisions are read in order beside its entries", async () => {
  const second = revision(2, { outcome: "withdrawn" });
  const first = revision(1, { matched_price: 2.12, stake: 0.8, bet_id: "P2", placed_utc: "2026-11-17T18:00:00Z" });
  const { supabase, requests } = stubbed(
    { real_alerts: [ALERT], real_entries: [ENTRY], real_entry_revisions: [second, first] },
    new Set(["anon"]),
  );
  const state = await loadReal(supabase);
  expect(state.ready && state.revisions).toEqual({ [ALERT.game_id]: [first, second] });
  const read = requests.find((r) => r.url.pathname.endsWith("/real_entry_revisions"));
  expect(read?.url.searchParams.get("game_date")).toBe("eq.2026-11-17");
});

test("an entry stands as its latest revision leaves it, and a withdrawal unrecords it", () => {
  expect(standing(ENTRY)).toEqual(ENTRY);
  const corrected = revision(1, { matched_price: 2.12, stake: 0.8, bet_id: "P2", placed_utc: "2026-11-17T18:00:00Z" });
  // The entry's own time and source stay: a revision records nothing late.
  expect(standing(ENTRY, [corrected])).toEqual({ ...ENTRY, matched_price: 2.12, stake: 0.8, bet_id: "P2" });
  const undone = revision(2, { outcome: "withdrawn" });
  expect(standing(ENTRY, [undone, corrected])).toBeNull();
  const notPlaced = revision(3, { outcome: "not placed", reason: "not matched" });
  expect(standing(ENTRY, [corrected, undone, notPlaced])).toEqual({
    ...ENTRY,
    outcome: "not placed",
    matched_price: null,
    stake: null,
    bet_id: null,
    placed_utc: null,
    reason: "not matched",
  });
});

test("a revision's note is required and kept short, as the migration checks it", () => {
  expect(checkNote("  ")).toMatch(/Say why/);
  expect(checkNote("x".repeat(NOTE_MAX + 1))).toMatch(/at most 200 characters/);
  expect(checkNote(" the stake was 0.80 ")).toBeNull();
});
