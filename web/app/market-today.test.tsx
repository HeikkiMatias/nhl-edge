// @vitest-environment jsdom
// Market making › Today (#398 step 3) on SYNTHETIC rows: the day's tiles, each game's card with
// its teams' prices, bids and holdings, the half-spread picker, a pulled game, the view that
// stopped updating, and the states before the migration and before any row; and the NHL's live
// scores on each card with where its holding stands (#408). The scoreboard route is stubbed.
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { liveRows } from "@/lib/fixtures/live-quotes";
import type { LiveGame as ScoreGame } from "@/lib/live";
import { stubbed } from "@/lib/testing";

import { MarketToday } from "./market-today";

/** The scoreboard the stubbed route serves: none by default, so the cards show no standing. */
let board: ScoreGame[] | Error = new Error("no scoreboard in this test");

beforeEach(() => {
  board = new Error("no scoreboard in this test");
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      board instanceof Error
        ? new Response(JSON.stringify({ error: board.message }), { status: 502 })
        : new Response(
            JSON.stringify({ date: "2026-11-05", fetched_utc: "2026-11-06T01:00:00Z", games: board }),
          ),
    ),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const card = (name: string) => screen.getByRole("article", { name });

// SYNTHETIC scoreboard games for the fixture's two: TOR at BOS, and SEA at VGK.
const game = (changes: Partial<ScoreGame> = {}): ScoreGame => ({
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

function client(rows = liveRows()) {
  return stubbed({ quote_live: rows }, new Set(["anon"]));
}

test("the day's totals and each game's card", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date("2026-11-05T22:05:00Z") });
  render(<MarketToday client={client().supabase} />);
  await screen.findByText("Our model at 1¢ below: today");
  expect(screen.getByText(/Paper only/)).toBeTruthy();
  const tile = (label: string) =>
    [...document.querySelectorAll(".tile")]
      .find((t) => t.querySelector(".label")?.textContent === label)
      ?.querySelector(".value")?.textContent;
  expect(tile("Games")).toBe("2");
  expect(tile("Fills")).toBe("2");
  expect(tile("Cost")).toBe("$7.14");
  expect(tile("If every result goes our way")).toBe("+$2.86");
  expect(tile("If every result goes against us")).toBe("−$3.14");
  const bos = card("TOR at BOS");
  expect(within(bos).getByText("Quoting")).toBeTruthy();
  // 19:00 ET on 11-05 is 02:00 on Friday in Helsinki, and the cycle 00:01 on Friday.
  expect(within(bos).getByText("Fri 02:00")).toBeTruthy();
  expect(screen.getByText(/updated Fri 00:01 Helsinki time/)).toBeTruthy();
  // The team names the score row too: the table's row is the one with a cell.
  const home = within(bos).getByRole("row", { name: /^BOS/ });
  expect(home.textContent).toContain("55¢");
  expect(home.textContent).toContain("52¢–54¢");
  expect(home.textContent).toContain("53¢ × 10");
  expect(home.textContent).toContain("10 at 53¢");
  expect(home.textContent).toContain("+$2.86");
  expect(within(bos).getByText(/4 contracts on both teams/)).toBeTruthy();
  const sea = card("SEA at VGK");
  expect(within(sea).getByText("Pulled: the price moved")).toBeTruthy();
  expect(screen.queryByText(/Not updating/)).toBeNull();
});

test("the picker shows another half-spread's quotes", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date("2026-11-05T22:05:00Z") });
  render(<MarketToday client={client().supabase} />);
  await screen.findByText("Our model at 1¢ below: today");
  fireEvent.click(screen.getByRole("radio", { name: "2¢" }));
  expect(screen.getByText("Our model at 2¢ below: today")).toBeTruthy();
  // Only BOS's market at 2¢ in the fixture: no whole game to show.
  expect(screen.queryByRole("article", { name: "TOR at BOS" })).toBeNull();
});

test("it reads again every minute, and says when the rows stopped coming", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date("2026-11-05T22:40:00Z") });
  const { supabase, requests } = client();
  render(<MarketToday client={supabase} />);
  await screen.findByText(/Not updating/);
  const reads = requests.length;
  await act(async () => {
    vi.advanceTimersByTime(60_000);
  });
  expect(requests.length).toBeGreaterThan(reads);
});

test("before the migration, and before any row", async () => {
  const { unmount } = render(<MarketToday client={stubbed({}, new Set(["anon"])).supabase} />);
  expect(await screen.findByText(/The live migration isn't applied yet/)).toBeTruthy();
  unmount();
  render(<MarketToday client={client([]).supabase} />);
  expect(await screen.findByText(/No live view yet/)).toBeTruthy();
});

test("each card carries the live score and where its holding stands; the day counts the games over", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date("2026-11-06T01:00:00Z") });
  board = [
    game(),
    game({ game_id: 2026020161, away: "SEA", home: "VGK", state: "OFF", away_score: 2, home_score: 4 }),
  ];
  render(<MarketToday client={client().supabase} />);
  const bos = await screen.findByRole("article", { name: "TOR at BOS" });
  await within(bos).findByText("BOS leads 3–1: +$2.86 if it ended now");
  // The score between the teams, and the period and clock.
  expect(within(bos).getByText("1–3")).toBeTruthy();
  expect(within(bos).getByText("2nd · 12:34")).toBeTruthy();
  const vgk = card("SEA at VGK");
  expect(within(vgk).getByText("2–4")).toBeTruthy();
  expect(within(vgk).getByText("Final")).toBeTruthy();
  expect(within(vgk).getByText("VGK won; nothing held")).toBeTruthy();
  const tile = (label: string) =>
    [...document.querySelectorAll(".tile")].find((t) => t.querySelector(".label")?.textContent === label);
  expect(tile("Finished today")?.querySelector(".value")?.textContent).toBe("$0.00");
  expect(tile("Finished today")?.querySelector(".sub")?.textContent).toBe(
    "1 of 2 over; provisional until the nightly settlement",
  );
  expect(tile("If every result goes our way")?.querySelector(".value")?.textContent).toBe("+$2.86");
  expect(screen.getByText(/A score never enters the record/)).toBeTruthy();
});

test("a game over counts at its result, provisional until the settlement", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date("2026-11-06T03:00:00Z") });
  // TOR won in the shootout: the full game counts, the shootout's winner credited one goal.
  board = [game({ state: "OFF", away_score: 4, home_score: 3, last_period_type: "SO" })];
  render(<MarketToday client={client().supabase} />);
  const bos = await screen.findByRole("article", { name: "TOR at BOS" });
  await within(bos).findByText("TOR won: −$3.14, provisional until tonight's settlement");
  expect(within(bos).getByText("Final (SO)")).toBeTruthy();
  // SEA-VGK isn't on this scoreboard: still to be decided.
  expect(within(card("SEA at VGK")).getByText("Not on the scoreboard yet")).toBeTruthy();
  const value = (label: string) =>
    [...document.querySelectorAll(".tile")]
      .find((t) => t.querySelector(".label")?.textContent === label)
      ?.querySelector(".value")?.textContent;
  expect(value("Finished today")).toBe("−$3.14");
  expect(value("If every result goes our way")).toBe("−$3.14");
  expect(value("If every result goes against us")).toBe("−$3.14");
});

test("without the scoreboard, the cards keep their start time and no standing", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date("2026-11-05T22:05:00Z") });
  render(<MarketToday client={client().supabase} />);
  expect(await screen.findByText(/Live scores unavailable: no scoreboard in this test/)).toBeTruthy();
  const bos = card("TOR at BOS");
  expect(within(bos).getByText("Fri 02:00")).toBeTruthy();
  expect(bos.querySelector(".now")).toBeNull();
  expect(screen.queryByText("Finished today")).toBeNull();
});
