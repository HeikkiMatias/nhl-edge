// @vitest-environment jsdom
// The real record section (#270): the start rule in plain words before the start, the practice
// record labelled as such, and the bankroll waiting for its migration.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import { ALERT, stubbed } from "@/lib/testing";
import type { Bet, LiveReport, RealStakes } from "@/lib/types";

import { RealRecord } from "./real-record";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(cleanup);

const BET: Bet = {
  game_date: ALERT.game_date,
  game_id: ALERT.game_id,
  start_utc: ALERT.start_utc,
  home: "TOR",
  away: "CHI",
  side: "home",
  price: 2.15,
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
};

const STAKES: RealStakes = {
  window: ["2026-11-16", "2026-11-30"],
  weekly: false,
  verdict: "not before 2026-11-16",
  failing: ["a", "d"],
  conditions: {
    a: {
      measured: false,
      holds: false,
      clv_per_bet: { games: 30, weeks: 2 },
      bound: { games: 30, weeks: 2 },
      share_with_proxy: 1,
      floor: 0.9,
    },
    c: { holds: true, undecided_days: [], unfinished_bundles: [] },
    d: { holds: false, venue: null },
  },
  veto: { paper_bankroll: 104.49, start: 100, applies: false },
  drawdown_review: { applies: false },
  clv_stop: { measured: false, weekly: false, stop: null, pause: null },
};

test("before the start: the rule in plain words and the practice record, labelled", async () => {
  const { supabase } = stubbed(
    { real_alerts: [ALERT], real_entries: [], real_settlements: [] },
    new Set(["anon"]),
  );
  const report = { real_stakes: STAKES } as unknown as LiveReport;
  render(<RealRecord client={supabase} bets={[BET]} report={report} />);
  expect(await screen.findByText(/Practice: nothing here is real/)).toBeTruthy();
  expect(screen.getByText(/not before 2026-11-16/)).toBeTruthy();
  expect(screen.getByText(/CLV \(a\): too few weeks to measure. CLV per bet 30 bets over 2 weeks/)).toBeTruthy();
  expect(screen.getByText("The venue (d): not chosen yet")).toBeTruthy();
  expect(screen.getByText(/Your veto: lifted/)).toBeTruthy();
  expect(screen.getByText("Not recorded")).toBeTruthy();
  expect(screen.getByText(/No settled bet yet/)).toBeTruthy();
});

test("without the settlements migration the bankroll says so, and the twins still show", async () => {
  const { supabase } = stubbed({ real_alerts: [ALERT], real_entries: [] }, new Set(["anon"]));
  render(<RealRecord client={supabase} bets={[BET]} report={null} />);
  expect(await screen.findByText(/20261016150000_real_record.sql/)).toBeTruthy();
  expect(screen.getByText("Not recorded")).toBeTruthy();
  expect(screen.getByText(/isn't in the latest report yet/)).toBeTruthy();
});

test("a measured CLV test shows both intervals it rests on (hard rule 7)", async () => {
  const { supabase } = stubbed({ real_alerts: [ALERT], real_entries: [] }, new Set(["anon"]));
  const measured: RealStakes = {
    ...STAKES,
    conditions: {
      ...STAKES.conditions,
      a: {
        measured: true,
        holds: false,
        clv_per_bet: { mean: 0.004, low: -0.008, high: 0.016, games: 120, weeks: 5 },
        bound: { mean: -0.002, low: -0.014, high: 0.01, games: 120, weeks: 5 },
        share_with_proxy: 0.95,
        floor: 0.9,
      },
    },
  };
  const report = { real_stakes: measured } as unknown as LiveReport;
  render(<RealRecord client={supabase} bets={[BET]} report={report} />);
  expect(
    await screen.findByText(
      /CLV per bet \+0.0040 \[-0.0080, \+0.0160\] \(120 bets, 5 weeks\); its pessimistic bound -0.0020 \[-0.0140, \+0.0100\]/,
    ),
  ).toBeTruthy();
});

test("after the start: the start date and the latest real day's mode", async () => {
  const real = [
    { ...ALERT, mode: "real", game_date: "2026-11-16", game_id: 1 },
    { ...ALERT, mode: "paused", game_date: "2026-11-24", game_id: 2, status: "skipped: real bets are paused" },
  ];
  const { supabase } = stubbed({ real_alerts: real, real_entries: [] }, new Set(["anon"]));
  render(<RealRecord client={supabase} bets={[]} report={null} />);
  expect(await screen.findByText(/Real bets started on/)).toBeTruthy();
  expect(screen.getByText("2026-11-16")).toBeTruthy();
  expect(screen.getByText(/On 2026-11-24, real bets are paused/)).toBeTruthy();
});

test("a bet a correction found not placed shows its cancelled settlement", async () => {
  const entry = {
    game_date: ALERT.game_date,
    game_id: ALERT.game_id,
    outcome: "placed",
    matched_price: 2.2,
    stake: 1.5,
    bet_id: "P1",
    placed_utc: "2026-11-17T18:00:00Z",
    reason: null,
    entered_utc: "2026-11-17T18:01:00Z",
    late: false,
  };
  const correction = {
    game_date: ALERT.game_date,
    game_id: ALERT.game_id,
    n: 0,
    changes: { outcome: "not placed", reason: "not matched" },
    reason: "statement",
    corrected_utc: "2026-11-23T10:00:00Z",
  };
  const cancelled = {
    game_date: ALERT.game_date,
    game_id: ALERT.game_id,
    revision: 1,
    mode: "practice",
    side: "home",
    matched_price: 2.2,
    stake: 1.5,
    status: "cancelled",
    won: null,
    profit: 0,
    result_utc: "2026-11-18T10:00:00Z",
    bankroll_after: 100,
  };
  const { supabase } = stubbed(
    {
      real_alerts: [ALERT],
      real_entries: [entry],
      real_corrections: [correction],
      real_settlements: [cancelled],
    },
    new Set(["anon"]),
  );
  render(<RealRecord client={supabase} bets={[BET]} report={null} />);
  expect(await screen.findByText("Cancelled by a correction")).toBeTruthy();
  expect(screen.getByText(/Not placed: not matched/)).toBeTruthy();
});
