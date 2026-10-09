import { expect, test } from "vitest";

import finals from "./fixtures/score-2026-10-08.json";
import upcoming from "./fixtures/score-2026-10-09.json";
import {
  dateAllowed,
  IDLE_POLL_MS,
  isCurrent,
  LIVE_POLL_MS,
  type LiveGame,
  nextPoll,
  parseScores,
  provisional,
  statusLine,
} from "./live.ts";
import type { Bet } from "./types.ts";

// The fixtures are the NHL's /v1/score answers as recorded on 2026-10-09: 2026-10-08's ten final
// games (SJS at STL went to overtime, TOR at VGK to a shootout) and 2026-10-09's four to come.
const NOW = new Date("2026-10-09T10:00:00Z");
const night = parseScores("2026-10-08", finals, NOW).games;
const tonight = parseScores("2026-10-09", upcoming, NOW).games;
const game = (games: LiveGame[], id: number) => games.find((g) => g.game_id === id) as LiveGame;

function bet(g: LiveGame, side: "home" | "away"): Bet {
  return {
    game_date: "2026-10-08",
    game_id: g.game_id,
    start_utc: g.start_utc,
    home: g.home,
    away: g.away,
    side,
    price: 1.9,
    p_side: 0.55,
    ev: 0.045,
    bankroll: 100,
    stake: 1,
    settled_utc: null,
    settlement: null,
    won: null,
    profit: null,
    close_status: null,
    clv: null,
    fair_move: null,
  };
}

test("the scoreboard keeps each game's teams, score, state and how it ended", () => {
  expect(night).toHaveLength(10);
  const overtime = game(night, 2026020063);
  expect([overtime.away, overtime.away_score, overtime.home, overtime.home_score]).toEqual([
    "SJS", 3, "STL", 2,
  ]);
  expect([overtime.state, overtime.last_period_type]).toEqual(["OFF", "OT"]);
  // A shootout's winner is credited one goal, so the score settles the full game.
  const shootout = game(night, 2026020065);
  expect([shootout.away_score, shootout.home_score, shootout.last_period_type]).toEqual([3, 4, "SO"]);
  // A game to come has no score yet.
  expect(tonight[0]).toMatchObject({ state: "FUT", away_score: null, home_score: null });
});

test("a game's state reads in words, from the start to the final", () => {
  expect(statusLine(game(night, 2026020056), "19:00")).toBe("Final");
  expect(statusLine(game(night, 2026020063), "20:00")).toBe("Final (OT)");
  expect(statusLine(game(night, 2026020065), "22:00")).toBe("Final (SO)");
  expect(statusLine(tonight[0], "19:00")).toBe("Starts 19:00 ET");
  // Live states, from a game to come with its state moved on as the scoreboard would.
  const on = (change: Partial<LiveGame>) => ({ ...tonight[0], away_score: 1, home_score: 2, ...change });
  expect(statusLine(on({ state: "PRE" }), "19:00")).toBe("Warm-up");
  expect(statusLine(on({ state: "LIVE", period: 2, period_type: "REG", clock: "12:34" }), "")).toBe(
    "2nd · 12:34",
  );
  expect(
    statusLine(on({ state: "LIVE", period: 2, period_type: "REG", clock: "00:00", intermission: true }), ""),
  ).toBe("2nd intermission");
  expect(statusLine(on({ state: "CRIT", period: 4, period_type: "OT", clock: "03:12" }), "")).toBe(
    "OT · 03:12",
  );
  expect(statusLine(on({ state: "CRIT", period: 5, period_type: "SO", clock: "00:00" }), "")).toBe(
    "Shootout",
  );
  expect(statusLine({ ...tonight[0], schedule_state: "PPD" }, "19:00")).toBe("Postponed");
});

test("a final score gives the bet a provisional result on the full game", () => {
  const overtime = game(night, 2026020063); // SJS won 3-2 in overtime, away
  expect(provisional(bet(overtime, "away"), overtime)).toBe("won");
  expect(provisional(bet(overtime, "home"), overtime)).toBe("lost");
  const shootout = game(night, 2026020065); // VGK won the shootout at home
  expect(provisional(bet(shootout, "home"), shootout)).toBe("won");
  // Nothing before the final, or for a game not played.
  const live = { ...tonight[0], state: "LIVE", away_score: 3, home_score: 0 };
  expect(provisional(bet(live, "away"), live)).toBeNull();
  const postponed = { ...overtime, schedule_state: "PPD" };
  expect(provisional(bet(postponed, "away"), postponed)).toBeNull();
  expect(provisional(bet(overtime, "away"), undefined)).toBeNull();
});

test("the board refreshes often while games are on, rarely before, and stops after", () => {
  expect(nextPoll(night, NOW)).toBeNull();
  // Tonight's first start is 23:00 UTC: hours away at 10:00, half an hour away at 22:30.
  expect(nextPoll(tonight, NOW)).toBe(IDLE_POLL_MS);
  expect(nextPoll(tonight, new Date("2026-10-09T22:30:00Z"))).toBe(LIVE_POLL_MS);
  const on = tonight.map((g, i) => (i === 0 ? { ...g, state: "LIVE" } : g));
  expect(nextPoll(on, NOW)).toBe(LIVE_POLL_MS);
  // A postponed or cancelled game is never waited for; a suspended one may resume, so it is
  // read again, at the slower rate.
  expect(nextPoll([{ ...tonight[0], schedule_state: "PPD" }], NOW)).toBeNull();
  expect(nextPoll([{ ...tonight[0], schedule_state: "CNCL" }], NOW)).toBeNull();
  const suspended = { ...tonight[0], state: "LIVE", schedule_state: "SUSP" };
  expect(nextPoll([suspended], NOW)).toBe(IDLE_POLL_MS);
  expect(statusLine(suspended, "19:00")).toBe("Suspended");
});

test("today's and last night's slates are current, by the Eastern calendar", () => {
  // 20:30 ET on 2026-10-08, and 02:00 ET the next morning while a late game may still run.
  expect(isCurrent("2026-10-08", new Date("2026-10-09T00:30:00Z"))).toBe(true);
  expect(isCurrent("2026-10-08", new Date("2026-10-09T06:00:00Z"))).toBe(true);
  expect(isCurrent("2026-10-07", new Date("2026-10-09T06:00:00Z"))).toBe(false);
  // 00:15 EDT on Monday 2027-03-15, the night the clocks went forward: Sunday's slate is last
  // night's, though 24 hours earlier was still Saturday in Eastern time.
  expect(isCurrent("2027-03-14", new Date("2027-03-15T04:15:00Z"))).toBe(true);
  expect(isCurrent("2027-03-13", new Date("2027-03-15T04:15:00Z"))).toBe(false);
});

test("the route serves real dates within two days of today only", () => {
  expect(dateAllowed("2026-10-09", "2026-10-09")).toBe(true);
  expect(dateAllowed("2026-10-07", "2026-10-09")).toBe(true);
  expect(dateAllowed("2026-10-12", "2026-10-09")).toBe(false);
  expect(dateAllowed("2026-02-30", "2026-02-28")).toBe(false);
  expect(dateAllowed("2026-10-09/../../v1/player", "2026-10-09")).toBe(false);
  expect(dateAllowed("", "2026-10-09")).toBe(false);
});
