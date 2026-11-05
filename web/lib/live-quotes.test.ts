// The live view (#398 step 3) on SYNTHETIC rows: the latest day under row-level security, each
// game's holding either way, the day's best and worst cases, and when the view looks stopped.
import { expect, test } from "vitest";

import { liveRow, liveRows } from "./fixtures/live-quotes.ts";
import { dayTotals, gamesOf, loadLive, priceCents, stale } from "./live-quotes.ts";
import { stubbed } from "./testing.ts";

const OWNER = new Set(["anon"]);

test("an owner reads the latest day's rows; a non-owner nothing; before the migration, says so", async () => {
  const { supabase, requests } = stubbed({ quote_live: liveRows() }, OWNER);
  const live = await loadLive(supabase);
  expect(live).toMatchObject({ applied: true, day: "2026-11-05" });
  expect(live.rows.length).toBe(5);
  const day = requests.filter((r) => r.url.searchParams.get("game_date"));
  // Every page of the rows is the latest day's.
  expect(new Set(day.map((r) => r.url.searchParams.get("game_date")))).toEqual(
    new Set(["eq.2026-11-05"]),
  );
  const stranger = stubbed({ quote_live: liveRows() }, new Set());
  expect(await loadLive(stranger.supabase)).toEqual({ applied: true, day: null, rows: [] });
  expect((await loadLive(stubbed({}, OWNER).supabase)).applied).toBe(false);
});

test("each game's holding pays either way, and the day adds them up", () => {
  const games = gamesOf(liveRows(), 1);
  expect(games.map((g) => g.game_id)).toEqual([2026020160, 2026020161]);
  const [bos, sea] = games;
  // 10 on BOS and 4 on TOR for $7.14: BOS winning pays $10, TOR winning $4.
  expect(bos.cost).toBeCloseTo(7.14);
  expect(bos.ifHome).toBeCloseTo(2.86);
  expect(bos.ifAway).toBeCloseTo(-3.14);
  expect(bos.pairs).toBe(4);
  expect([sea.ifHome, sea.ifAway, sea.pairs]).toEqual([0, 0, 0]);
  const t = dayTotals(games);
  expect(t).toMatchObject({ games: 2, quoting: 1, fills: 2, contracts: 14 });
  expect(t.best).toBeCloseTo(2.86);
  expect(t.worst).toBeCloseTo(-3.14);
  // The other half-spread's rows are its own.
  expect(gamesOf(liveRows(), 2)).toEqual([]);
});

test("the view looks stopped only when old while a game is still to start", () => {
  const rows = [liveRow()];
  expect(stale(rows, new Date("2026-11-05T22:10:00Z"))).toBe(false);
  expect(stale(rows, new Date("2026-11-05T22:30:00Z"))).toBe(true);
  // Every game started: nothing more to write.
  expect(stale(rows, new Date("2026-11-06T01:00:00Z"))).toBe(false);
});

test("prices read in cents", () => {
  expect(priceCents(0.53)).toBe("53¢");
  expect(priceCents(null)).toBe("–");
});
