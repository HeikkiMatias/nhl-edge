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
    a: { measured: false, holds: false, share_with_proxy: 1, floor: 0.9 },
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
  expect(screen.getByText("CLV (a): too few weeks to measure")).toBeTruthy();
  expect(screen.getByText("The venue (d): not chosen yet")).toBeTruthy();
  expect(screen.getByText(/Your veto: lifted/)).toBeTruthy();
  expect(screen.getByText("Not recorded")).toBeTruthy();
  expect(screen.getByText(/No settled bet yet/)).toBeTruthy();
});

test("without the settlements migration the bankroll says so, and the twins still show", async () => {
  const { supabase } = stubbed({ real_alerts: [ALERT], real_entries: [] }, new Set(["anon"]));
  render(<RealRecord client={supabase} bets={[BET]} report={null} />);
  expect(await screen.findByText(/20261016150000_real_settlements.sql/)).toBeTruthy();
  expect(screen.getByText("Not recorded")).toBeTruthy();
  expect(screen.getByText(/isn't in the latest report yet/)).toBeTruthy();
});
