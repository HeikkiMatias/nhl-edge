// The real record's sums (#270): each paper bet's twin, the bankroll by each bet's latest
// settlement revision, the price against the paper price, and practice kept apart from real.
import { expect, test } from "vitest";

import { corrected, priceGap, realBankroll, realRecord, since, twins, view } from "./real-record.ts";
import { ALERT } from "./testing.ts";
import type { Bet, RealAlert, RealEntry, RealSettlement } from "./types.ts";

const bet = (game_id: number, game_date = "2026-11-17", price = 2.15): Bet => ({
  game_date,
  game_id,
  start_utc: "2026-11-18T00:00:00Z",
  home: "TOR",
  away: "CHI",
  side: "home",
  price,
  p_side: 0.5,
  ev: 0.075,
  bankroll: 100,
  stake: 1.5,
  settled_utc: null,
  settlement: null,
  won: null,
  profit: null,
  close_status: null,
  clv: null,
  fair_move: null,
  policy_version: "policy-v2",
});
const alert = (game_id: number, changes: Partial<RealAlert> = {}): RealAlert => ({
  ...ALERT,
  game_id,
  mode: "real",
  ...changes,
});
const placed = (game_id: number, price: number, stake: number): RealEntry => ({
  game_date: "2026-11-17",
  game_id,
  outcome: "placed",
  matched_price: price,
  stake,
  bet_id: `P${game_id}`,
  placed_utc: "2026-11-17T18:00:00Z",
  reason: null,
  entered_utc: "2026-11-17T18:01:00Z",
  late: false,
});
const settled = (game_id: number, changes: Partial<RealSettlement> = {}): RealSettlement => ({
  game_date: "2026-11-17",
  game_id,
  revision: 0,
  mode: "real",
  side: "home",
  matched_price: 2.2,
  stake: 1.5,
  status: "settled",
  won: true,
  profit: 1.8,
  result_utc: "2026-11-18T10:00:00Z",
  bankroll_after: 101.8,
  ...changes,
});

test("each paper bet's twin reads placed, not placed, skipped, not recorded or no alert", () => {
  const bets = [1, 2, 3, 4, 5].map((g) => bet(g)).concat(bet(9, "2026-11-10"));
  const alerts = [
    alert(1),
    alert(2),
    alert(3, { status: "skipped: the stake is below the venue's minimum" }),
    alert(4),
  ];
  const entries: RealEntry[] = [
    placed(1, 2.2, 1.5),
    { ...placed(2, 0, 0), outcome: "not placed", matched_price: null, stake: null, reason: "not matched" },
  ];
  const rows = twins(bets, alerts, entries, [settled(1)], "real");
  expect(rows.map((t) => [t.bet.game_id, t.state])).toEqual([
    [1, "placed"],
    [2, "not placed"],
    [3, "skipped"],
    [4, "not recorded"],
    [5, "no alert"],
  ]);
  expect(rows[0].settlement?.profit).toBe(1.8);
});

test("the bankroll counts each bet's latest revision, real apart from practice", () => {
  const rows = [
    settled(1),
    // The weekly check found the stake was 1.00: the second revision counts.
    settled(1, { revision: 1, stake: 1.0, profit: 1.2, bankroll_after: 101.2 }),
    settled(2, { won: false, profit: -1.5, game_date: "2026-11-18" }),
    settled(3, { mode: "practice", profit: 5 }),
  ];
  const days = realBankroll(rows, "real");
  expect(days.map((d) => [d.date, d.bets, d.profit])).toEqual([
    ["2026-11-17", 1, 1.2],
    ["2026-11-18", 1, -1.5],
  ]);
  expect(days.map((d) => d.balance)).toEqual([101.2, expect.closeTo(99.7)]);
  const r = realRecord(rows, "real");
  expect([r.won, r.lost, r.void, r.payout, r.staked]).toEqual([1, 1, 0, 0, 2.5]);
  expect(r.profit).toBeCloseTo(-0.3);
  expect(realBankroll(rows, "practice")).toEqual([
    { date: "2026-11-17", bets: 1, profit: 5, balance: 105 },
  ]);
});

test("a bet paid out at Kalshi's own price counts in the bankroll, apart from won and lost", () => {
  // A Kalshi game played over two weeks late, or never, is paid out at Kalshi's price (#290).
  const rows = [
    settled(1),
    settled(2, { status: "payout", won: null, profit: -0.69, game_date: "2026-11-18" }),
  ];
  expect(realBankroll(rows, "real").map((d) => [d.date, d.bets, d.profit])).toEqual([
    ["2026-11-17", 1, 1.8],
    ["2026-11-18", 1, -0.69],
  ]);
  const r = realRecord(rows, "real");
  expect([r.won, r.lost, r.void, r.payout, r.staked]).toEqual([1, 0, 0, 1, 3]);
  expect(r.profit).toBeCloseTo(1.11);
});

test("the price gap compares the matched price with the paper price", () => {
  const rows = twins(
    [bet(1), bet(2)],
    [alert(1), alert(2)],
    [placed(1, 2.2, 1.5), placed(2, 2.1, 1.0)],
    [settled(1), settled(2, { won: false, profit: -1.0, matched_price: 2.1, stake: 1.0 })],
    "real",
  );
  const gap = priceGap(rows);
  expect(gap.bets.map((b) => Number(b.difference.toFixed(3)))).toEqual([0.05, -0.05]);
  expect(gap.mean).toBeCloseTo(0);
  // Only the won bet's price matters: 1.5 × (2.20 − 2.15).
  expect(gap.profit).toBeCloseTo(0.075);
});

test("the view is practice until a day is alerted for real, and practice ends where real begins", () => {
  const practice = [alert(1, { mode: "practice", game_date: "2026-11-10" })];
  expect(view(practice)).toBe("practice");
  const both = [...practice, alert(2, { game_date: "2026-11-17" })];
  expect(view(both)).toBe("real");
  const bets = [bet(2, "2026-11-17"), bet(1, "2026-11-10")];
  expect(twins(bets, both, [], [], "real").map((t) => t.bet.game_id)).toEqual([2]);
  expect(twins(bets, both, [], [], "practice").map((t) => t.bet.game_id)).toEqual([1]);
});

test("corrections apply in order over the entries, as the R2 record applies them", () => {
  const alerts = [alert(1), alert(2), alert(3)];
  const entries = [placed(1, 2.2, 1.0), placed(3, 2.2, 1.0)];
  const fixed = corrected(
    entries,
    [
      { game_date: "2026-11-17", game_id: 1, n: 1, changes: { stake: 1.3 }, reason: "b", corrected_utc: "2026-11-23T10:01:00Z" },
      { game_date: "2026-11-17", game_id: 1, n: 0, changes: { stake: 1.2 }, reason: "a", corrected_utc: "2026-11-23T10:00:00Z" },
      // A bet placed but never recorded, added at the weekly check.
      {
        game_date: "2026-11-17",
        game_id: 2,
        n: 0,
        changes: { outcome: "placed", matched_price: 2.1, stake: 1.0, bet_id: "X2", placed_utc: "2026-11-17T18:00:00Z" },
        reason: "c",
        corrected_utc: "2026-11-23T10:00:00Z",
      },
      { game_date: "2026-11-17", game_id: 3, n: 0, changes: { outcome: "not placed", reason: "not matched" }, reason: "d", corrected_utc: "2026-11-23T10:00:00Z" },
    ],
    alerts,
  );
  const rows = twins([bet(1), bet(2), bet(3)], alerts, fixed.entries, [], "real");
  expect(rows.map((t) => [t.state, t.entry?.stake ?? null])).toEqual([
    ["placed", 1.3],
    ["placed", 1.0],
    ["not placed", null],
  ]);
  expect(rows[1].entry?.late).toBe(true);
  expect(fixed.counts.get("2026-11-17/1")).toBe(2);
});

test("a closed window's alerts end the practice history, and the start reads from the alerts", () => {
  const closed = alert(1, { mode: "closed", status: "skipped: the start window closed without a start", game_date: "2026-12-01" });
  expect(view([closed])).toBe("practice");
  expect(twins([bet(1, "2026-12-01")], [closed], [], [], "practice").map((t) => t.state)).toEqual(["skipped"]);
  expect(since([closed])).toBeNull();
  const started = since([alert(1, { game_date: "2026-11-23" }), alert(2, { game_date: "2026-11-16", mode: "real" })]);
  expect(started?.start).toBe("2026-11-16");
  expect(started?.latest).toBe("2026-11-23");
  // The practice history ends on the day the window closed: no alert runs after it.
  const practice = alert(3, { mode: "practice", game_date: "2026-11-20" });
  const rows = twins(
    [bet(5, "2026-12-03"), bet(1, "2026-12-01"), bet(3, "2026-11-20")],
    [practice, closed],
    [],
    [],
    "practice",
  );
  expect(rows.map((t) => [t.bet.game_date, t.state])).toEqual([
    ["2026-12-01", "skipped"],
    ["2026-11-20", "not recorded"],
  ]);
});

test("a day without bets still sets the record's mode, from real_days", () => {
  // The start fell on a day without bets, and a later day without bets paused real bets.
  const days = [
    { game_date: "2026-11-16", mode: "real" as const, started: "2026-11-16", reason: "" },
    { game_date: "2026-11-24", mode: "paused" as const, started: "2026-11-16", reason: "floor" },
  ];
  const alerts = [alert(1, { game_date: "2026-11-18" })];
  expect(view([], days)).toBe("real");
  expect(since(alerts, days)).toEqual({ start: "2026-11-16", latest: "2026-11-24", mode: "paused" });
});
