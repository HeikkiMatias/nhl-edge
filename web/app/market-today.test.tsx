// @vitest-environment jsdom
// Market making › Today (#398 step 3) on SYNTHETIC rows: the day's tiles, each game's card with
// its teams' prices, bids and holdings, the half-spread picker, a pulled game, the view that
// stopped updating, and the states before the migration and before any row.
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { liveRows } from "@/lib/fixtures/live-quotes";
import { stubbed } from "@/lib/testing";

import { MarketToday } from "./market-today";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
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
  const bos = screen.getByRole("heading", { name: "TOR at BOS" }).closest(".card") as HTMLElement;
  expect(within(bos).getByText("Quoting")).toBeTruthy();
  // 19:00 ET on 11-05 is 02:00 on Friday in Helsinki, and the cycle 00:01 on Friday.
  expect(within(bos).getByText("Fri 02:00")).toBeTruthy();
  expect(screen.getByText(/updated Fri 00:01 Helsinki time/)).toBeTruthy();
  const home = within(bos).getByText("BOS").closest("tr") as HTMLElement;
  expect(home.textContent).toContain("55¢");
  expect(home.textContent).toContain("52¢–54¢");
  expect(home.textContent).toContain("53¢ × 10");
  expect(home.textContent).toContain("10 at 53¢");
  expect(home.textContent).toContain("+$2.86");
  expect(within(bos).getByText(/4 contracts on both teams/)).toBeTruthy();
  const sea = screen.getByRole("heading", { name: "SEA at VGK" }).closest(".card") as HTMLElement;
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
  expect(screen.queryByRole("heading", { name: "TOR at BOS" })).toBeNull();
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
