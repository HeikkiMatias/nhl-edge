// The live view (#398 step 3) on SYNTHETIC rows: the latest day under row-level security, each
// game's holding either way, the day's best and worst cases, and when the view looks stopped.
import { expect, test } from "vitest";

import { liveRow, liveRows } from "./fixtures/live-quotes.ts";
import type { LiveGame as ScoreGame } from "./live.ts";
import { dayTotals, gamesOf, loadLive, priceCents, stale, standing } from "./live-quotes.ts";
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

// A SYNTHETIC scoreboard game for BOS-TOR (TOR at BOS), under way in the 2nd.
const score = (changes: Partial<ScoreGame> = {}): ScoreGame => ({
  game_id: 2026020160,
  start_utc: "2026-11-06T00:00:00Z",
  state: "LIVE",
  schedule_state: "OK",
  away: "TOR",
  home: "BOS",
  away_score: 1,
  home_score: 3,
  period: 2,
  period_type: "REG",
  clock: "12:34",
  intermission: false,
  last_period_type: null,
  ...changes,
});

test("a holding's standing on the scoreboard, as the Betting view words a bet's (#408)", () => {
  const [bos, sea] = gamesOf(liveRows(), 1);
  // 10 on BOS and 4 on TOR for $7.14: +$2.86 if BOS wins, −$3.14 if TOR does.
  expect(standing(bos, undefined)).toEqual({
    tone: "waiting",
    text: "Not on the scoreboard yet",
    result: null,
  });
  expect(standing(bos, score({ state: "FUT", home_score: null, away_score: null })).text).toBe(
    "Not started yet",
  );
  expect(standing(bos, score())).toEqual({
    tone: "live",
    text: "BOS leads 3–1: +$2.86 if it ended now",
    result: null,
  });
  expect(standing(bos, score({ away_score: 4 })).text).toBe("TOR leads 4–3: −$3.14 if it ended now");
  expect(standing(bos, score({ away_score: 3 })).text).toBe(
    "Tied 3–3: +$2.86 if BOS wins, −$3.14 if TOR wins",
  );
  // Over on the full game: the shootout's winner is credited one goal.
  const final = score({ state: "OFF", away_score: 4, home_score: 3, last_period_type: "SO" });
  expect(standing(bos, final)).toEqual({
    tone: "lost",
    text: "TOR won: −$3.14, provisional until tonight's settlement",
    result: bos.ifAway,
  });
  expect(standing(bos, score({ state: "FINAL" }))).toMatchObject({ tone: "won", result: bos.ifHome });
  expect(standing(bos, score({ schedule_state: "PPD" }))).toEqual({
    tone: "off",
    text: "Postponed: the nightly settlement decides",
    result: null,
  });
  // Nothing held: the game still shows, and counts nothing once over.
  const vgk = score({ game_id: sea.game_id, away: "SEA", home: "VGK" });
  expect(standing(sea, vgk).text).toBe("Under way; nothing held");
  expect(standing(sea, { ...vgk, state: "OFF" })).toEqual({
    tone: "off",
    text: "VGK won; nothing held",
    result: 0,
  });
});

test("with the scoreboard, the day counts the games over and the best and worst run from there", () => {
  const games = gamesOf(liveRows(), 1);
  const [bos, sea] = games;
  // Before the scoreboard is read, or with BOS-TOR still on, nothing is over.
  expect(dayTotals(games, null)).toMatchObject({ finished: 0, over: 0 });
  const on = dayTotals(games, new Map([[bos.game_id, score()]]));
  expect(on).toMatchObject({ finished: 0, over: 0 });
  expect(on.best).toBeCloseTo(2.86);
  // TOR won BOS-TOR: −$3.14 finished, and SEA-VGK (nothing held) still to come.
  const lost = dayTotals(
    games,
    new Map([
      [bos.game_id, score({ state: "OFF", away_score: 4, home_score: 3 })],
      [sea.game_id, score({ game_id: sea.game_id, state: "FUT", away_score: null, home_score: null })],
    ]),
  );
  expect(lost.over).toBe(1);
  expect(lost.finished).toBeCloseTo(-3.14);
  expect(lost.best).toBeCloseTo(-3.14);
  expect(lost.worst).toBeCloseTo(-3.14);
});
