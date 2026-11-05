// @vitest-environment jsdom
// Live games and our bets (#398 step 4): a game's card shows the bet recorded on the form, in
// euros, ahead of the paper bet, and the day's totals count the recorded bets. The scoreboard is
// the app's own route, stubbed; every figure is synthetic.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { LiveGame } from "@/lib/live";
import type { RealState } from "@/lib/real";
import { ALERT } from "@/lib/testing";
import type { Prediction } from "@/lib/types";

import { LiveScores } from "./live";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const GAME: LiveGame = {
  game_id: ALERT.game_id,
  start_utc: ALERT.start_utc,
  state: "LIVE",
  schedule_state: "OK",
  away: "CHI",
  home: "TOR",
  away_score: 2,
  home_score: 1,
  period: 3,
  period_type: "REG",
  clock: "05:00",
  intermission: false,
  last_period_type: null,
};
const ROW: Prediction = {
  game_date: ALERT.game_date,
  game_id: ALERT.game_id,
  start_utc: ALERT.start_utc,
  home: "TOR",
  away: "CHI",
  status: "bet",
  home_price: 2.15,
  away_price: 1.8,
  p_b1: 0.45,
  p_b3: 0.48,
  p_blend: 0.5,
  bet: true,
};
const REAL: RealState = {
  ready: true,
  day: ALERT.game_date,
  alerts: [ALERT],
  entries: {
    [ALERT.game_id]: {
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
    },
  },
  revisions: {},
  corrections: {},
};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-18T02:00:00Z"));
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ date: ALERT.game_date, fetched_utc: "2026-11-18T02:00:00Z", games: [GAME] }))),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

test("a game shows the practice bet recorded on the form, with what it pays and where it stands", async () => {
  render(<LiveScores day={ALERT.game_date} rows={[ROW]} bets={[]} real={REAL} />);
  const card = (await screen.findByText("3rd · 05:00")).closest("article") as HTMLElement;
  expect(card.className).toBe("live-game has-bet");
  expect(card.textContent).toContain("Practice bet: TOR at 2.1, stake 0.90 €");
  expect(card.querySelector(".pays")?.textContent).toBe("If it wins+0.99 €If it loses−0.90 €");
  expect(card.querySelector(".now")?.textContent).toBe("Trailing 1–2: would lose −0.90 € if it ended now");
  // The totals count the recorded bet in euros.
  const tiles = screen.getByLabelText("Today's totals: the practice bets recorded");
  expect(tiles.textContent).toContain("Staked in play0.90 €");
  expect(tiles.textContent).toContain("If all open bets win+0.99 €");
  expect(tiles.textContent).toContain("If all open bets lose−0.90 €");
});

test("a game without a bet says so, and no totals show", async () => {
  render(<LiveScores day={ALERT.game_date} rows={[ROW]} bets={[]} real={null} />);
  expect(await screen.findByText("No bet on this game.")).toBeTruthy();
  expect(screen.queryByText("Staked in play")).toBeNull();
});
