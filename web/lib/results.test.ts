import { expect, test } from "vitest";

import {
  bankroll,
  betsPerDay,
  policyNames,
  policyStarts,
  record,
  result,
  scoreLine,
  START,
} from "./results.ts";
import type { Bet } from "./types.ts";

const V1 = "policy-20261005-8ec5cf3";
const V2 = "policy-20261014-e15db87";

function bet(game_date: string, game_id: number, settled: Partial<Bet>): Bet {
  return {
    game_date,
    game_id,
    start_utc: `${game_date}T23:00:00+00:00`,
    home: "BUF",
    away: "DAL",
    side: "away",
    price: 1.88,
    p_side: 0.58,
    ev: 0.09,
    bankroll: 100,
    stake: 1,
    settled_utc: null,
    settlement: null,
    won: null,
    profit: null,
    close_status: null,
    clv: null,
    fair_move: null,
    policy_version: V1,
    ...settled,
  };
}

const won = (profit: number) => ({ settlement: "settled", won: true, profit });
const lost = (stake: number) => ({ settlement: "settled", won: false, profit: -stake, stake });

test("the bankroll moves by each settled day's profit, from 100 units", () => {
  const days = bankroll([
    bet("2026-10-09", 3, lost(1)),
    bet("2026-10-08", 1, won(0.88)),
    bet("2026-10-08", 2, lost(1.09)),
    bet("2026-10-10", 4, {}), // awaiting its result
    bet("2026-10-10", 5, { settlement: "void", profit: 0 }),
  ]);
  expect(days.map((d) => d.date)).toEqual(["2026-10-08", "2026-10-09"]);
  expect(days[0].bets).toBe(2);
  expect(days[0].balance).toBeCloseTo(START + 0.88 - 1.09, 10);
  expect(days[1].balance).toBeCloseTo(START + 0.88 - 1.09 - 1, 10);
  // Below the start all along, so the peak stays the start.
  expect(days[1].peak).toBe(START);
  expect(days[1].drawdown).toBeCloseTo(days[1].balance / START - 1, 10);
});

test("the drawdown is measured from the running peak", () => {
  const days = bankroll([
    bet("2026-10-08", 1, won(10)),
    bet("2026-10-09", 2, lost(22)),
  ]);
  expect(days[0].peak).toBe(110);
  expect(days[1].balance).toBe(88);
  expect(days[1].drawdown).toBeCloseTo(88 / 110 - 1, 10);
});

test("the record counts won, lost, void and awaiting bets", () => {
  const bets = [
    bet("2026-10-08", 1, won(0.88)),
    bet("2026-10-08", 2, lost(1.09)),
    bet("2026-10-09", 3, { settlement: "void", profit: 0 }),
    bet("2026-10-09", 4, {}),
  ];
  const r = record(bets);
  expect([r.won, r.lost, r.void, r.open]).toEqual([1, 1, 1, 1]);
  expect(r.profit).toBeCloseTo(0.88 - 1.09, 10);
  expect(r.staked).toBeCloseTo(1 + 1.09, 10);
  expect(r.inPlay).toBe(1);
  expect(bets.map(result)).toEqual(["Won", "Lost", "Void", "Awaiting the result"]);
});

test("bets per day are counted oldest first, settled or not", () => {
  const bets = [bet("2026-10-09", 3, {}), bet("2026-10-08", 1, {}), bet("2026-10-08", 2, {})];
  expect(betsPerDay(bets)).toEqual([
    { date: "2026-10-08", bets: 2 },
    { date: "2026-10-09", bets: 1 },
  ]);
});

test("a score reads away first, with overtime or a shootout marked", () => {
  const b = bet("2026-10-08", 1, {});
  expect(scoreLine(b, { game_id: 1, away_score: 4, home_score: 2, decided_in: "REG" })).toBe(
    "DAL 4–2 BUF",
  );
  expect(scoreLine(b, { game_id: 1, away_score: 2, home_score: 3, decided_in: "SO" })).toBe(
    "DAL 2–3 BUF (SO)",
  );
  expect(scoreLine(b, undefined)).toBe("");
});

test("one bankroll runs through the policies, and each later policy's first day is marked", () => {
  // ADR 0037: v2 carries on from v1's balance, so its days continue the same line.
  const bets = [
    bet("2026-10-15", 3, { ...lost(1), policy_version: V2 }),
    bet("2026-10-14", 1, won(2)),
    bet("2026-10-16", 4, { ...won(0.5), policy_version: V2 }),
    bet("2026-10-17", 5, { policy_version: V2 }), // awaiting its result
  ];
  const names = policyNames(bets);
  expect([...names.entries()]).toEqual([
    [V1, "v1"],
    [V2, "v2"],
  ]);
  const days = bankroll(bets);
  expect(days.map((d) => [d.date, d.policy])).toEqual([
    ["2026-10-14", V1],
    ["2026-10-15", V2],
    ["2026-10-16", V2],
  ]);
  expect(days[1].balance).toBeCloseTo(START + 2 - 1, 10);
  expect(days[1].peak).toBe(START + 2);
  expect(policyStarts(days)).toEqual([1]);
  // One policy only: no marker.
  expect(policyStarts(bankroll([bet("2026-10-14", 1, won(2))]))).toEqual([]);
});
