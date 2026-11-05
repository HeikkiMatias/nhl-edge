// Live games and our bets (#398 step 4): which bet a game shows, where it stands on the scoreboard,
// and the day's totals. Synthetic games and bets only.
import { expect, test } from "vitest";

import type { LiveGame } from "./live.ts";
import { money, type OurBet, ourBets, pays, rightNow, totals } from "./live-bets.ts";
import type { RealState } from "./real.ts";
import { ALERT } from "./testing.ts";
import type { Bet, RealEntry } from "./types.ts";

const DAY = ALERT.game_date;

const paper = (game_id: number, changes: Partial<Bet> = {}): Bet => ({
  game_date: DAY,
  game_id,
  start_utc: ALERT.start_utc,
  home: "TOR",
  away: "CHI",
  side: "home",
  price: 2.0,
  p_side: 0.55,
  ev: 0.1,
  bankroll: 100,
  stake: 1.0,
  settled_utc: null,
  settlement: null,
  won: null,
  profit: null,
  close_status: null,
  clv: null,
  fair_move: null,
  policy_version: "policy-v2",
  ...changes,
});

const game = (changes: Partial<LiveGame> = {}): LiveGame => ({
  game_id: ALERT.game_id,
  start_utc: ALERT.start_utc,
  state: "LIVE",
  schedule_state: "OK",
  away: "CHI",
  home: "TOR",
  away_score: 1,
  home_score: 2,
  period: 2,
  period_type: "REG",
  clock: "10:00",
  intermission: false,
  last_period_type: null,
  ...changes,
});

const ENTRY: RealEntry = {
  game_date: DAY,
  game_id: ALERT.game_id,
  outcome: "placed",
  matched_price: 2.1,
  stake: 0.9,
  bet_id: "P1",
  placed_utc: "2026-11-17T18:00:00Z",
  reason: null,
  entered_utc: "2026-11-17T18:01:00Z",
  late: false,
};

const real = (changes: Partial<Extract<RealState, { ready: true }>> = {}): RealState => ({
  ready: true,
  day: DAY,
  alerts: [ALERT],
  entries: { [ALERT.game_id]: ENTRY },
  revisions: {},
  corrections: {},
  ...changes,
});

test("a game shows the bet recorded on the form, or else the paper bet", () => {
  const bets = [paper(ALERT.game_id), paper(2026020251, { side: "away" })];
  const shown = ourBets(DAY, bets, real());
  expect(shown.get(ALERT.game_id)).toEqual({
    kind: "practice",
    game_id: ALERT.game_id,
    side: "home",
    team: "TOR",
    price: 2.1,
    stake: 0.9,
    unit: "€",
  });
  expect(shown.get(2026020251)).toMatchObject({ kind: "paper", team: "CHI", unit: "units" });
  // A real day's entry is "real"; another day's form, or none, leaves the paper bets.
  expect(ourBets(DAY, bets, real({ alerts: [{ ...ALERT, mode: "real" }] })).get(ALERT.game_id)?.kind).toBe("real");
  expect(ourBets(DAY, bets, real({ day: "2026-11-16" })).get(ALERT.game_id)?.kind).toBe("paper");
  expect(ourBets(DAY, bets, null).get(ALERT.game_id)?.kind).toBe("paper");
});

test("the recorded bet is the entry as its revisions and corrections leave it", () => {
  const revision = {
    game_date: DAY,
    game_id: ALERT.game_id,
    n: 1,
    outcome: "placed" as const,
    matched_price: 2.12,
    stake: 0.8,
    bet_id: "P2",
    placed_utc: "2026-11-17T18:00:00Z",
    reason: null,
    note: "typo",
    revised_utc: "2026-11-17T18:05:00Z",
  };
  const revised = ourBets(DAY, [], real({ revisions: { [ALERT.game_id]: [revision] } }));
  expect(revised.get(ALERT.game_id)).toMatchObject({ price: 2.12, stake: 0.8 });
  const fixed = ourBets(DAY, [], real({
    corrections: {
      [ALERT.game_id]: [{ game_date: DAY, game_id: ALERT.game_id, n: 0, changes: { stake: 0.7 }, reason: "statement", corrected_utc: "2026-11-23T10:00:00Z" }],
    },
  }));
  expect(fixed.get(ALERT.game_id)?.stake).toBe(0.7);
  // Withdrawn, or not placed: the paper bet shows instead.
  const undone = ourBets(DAY, [paper(ALERT.game_id)], real({
    revisions: { [ALERT.game_id]: [{ ...revision, outcome: "withdrawn", matched_price: null, stake: null, bet_id: null, placed_utc: null }] },
  }));
  expect(undone.get(ALERT.game_id)?.kind).toBe("paper");
  const notPlaced = ourBets(DAY, [], real({ entries: { [ALERT.game_id]: { ...ENTRY, outcome: "not placed", matched_price: null, stake: null, bet_id: null, placed_utc: null, reason: "missed it" } } }));
  expect(notPlaced.size).toBe(0);
});

const BET: OurBet = { kind: "practice", game_id: ALERT.game_id, side: "home", team: "TOR", price: 2.1, stake: 0.9, unit: "€" };

test("right now says where the bet stands, and what it would pay if the game ended then", () => {
  expect(pays(BET)).toBeCloseTo(0.99);
  expect(rightNow(BET, undefined)).toEqual({ tone: "waiting", text: "Not on the scoreboard yet", result: null });
  expect(rightNow(BET, game({ state: "FUT", away_score: null, home_score: null })).text).toBe("Not started yet");
  expect(rightNow(BET, game()).text).toBe("Leading 2–1: would win +0.99 € if it ended now");
  expect(rightNow({ ...BET, side: "away", team: "CHI" }, game()).text).toBe(
    "Trailing 1–2: would lose −0.90 € if it ended now",
  );
  expect(rightNow(BET, game({ away_score: 2 })).text).toBe("Tied 2–2: overtime or a shootout would decide");
  // Over on the full game: the shootout's winner is credited one goal.
  const final = game({ state: "OFF", away_score: 3, home_score: 4, last_period_type: "SO" });
  expect(rightNow(BET, final)).toEqual({
    tone: "won",
    text: "Won +0.99 €, provisional until tonight's settlement",
    result: 0.9 * 1.1,
  });
  expect(rightNow({ ...BET, side: "away" }, final)).toMatchObject({ tone: "lost", result: -0.9 });
  expect(rightNow(BET, game({ schedule_state: "PPD" })).text).toBe("Postponed: the nightly settlement decides");
});

test("a settled paper bet shows its official result, whatever the scoreboard says", () => {
  const settled = (changes: Partial<Bet>): OurBet => ({ ...BET, kind: "paper", unit: "units", paper: paper(ALERT.game_id, changes) });
  expect(rightNow(settled({ settlement: "settled", won: true, profit: 1.1 }), undefined)).toEqual({
    tone: "won",
    text: "Won +1.10 units, settled",
    result: 1.1,
  });
  expect(rightNow(settled({ settlement: "settled", won: false, profit: -0.9 }), game()).tone).toBe("lost");
  expect(rightNow(settled({ settlement: "void", profit: 0 }), game())).toMatchObject({ tone: "off", result: 0 });
});

test("the totals add the open and finished bets, in euros where any bet is recorded", () => {
  const other: OurBet = { ...BET, game_id: 2, stake: 0.5, price: 3.0 };
  const games = new Map([
    [BET.game_id, game({ state: "FINAL", away_score: 0, home_score: 1 })],
    [2, game({ game_id: 2 })],
  ]);
  const paperBet: OurBet = { ...BET, game_id: 3, kind: "paper", unit: "units" };
  expect(totals([BET, other, paperBet], games)).toEqual({
    unit: "€",
    kind: "practice",
    bets: 2,
    inPlay: 0.5,
    best: 0.9 * 1.1 + 1.0,
    worst: 0.9 * 1.1 - 0.5,
    finished: 0.9 * 1.1,
    over: 1,
  });
  // Without a recorded bet, the paper bets in units.
  expect(totals([paperBet], games)).toMatchObject({ unit: "units", kind: "paper", inPlay: 0.9 });
  expect(totals([], games)).toBeNull();
  expect(money(-0.5, "€")).toBe("−0.50 €");
  expect(money(0.5, "units", false)).toBe("0.50 units");
});
