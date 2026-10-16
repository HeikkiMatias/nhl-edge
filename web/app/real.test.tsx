// @vitest-environment jsdom
// The real bets section (#268): each alerted bet's minimum price and the stake at the price
// typed, the entry sent to real_entries as the migration takes it, the database's refusal shown,
// and the recorded entry once read back. Supabase is the real client over the PostgREST stub.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ALERT, type Answer, type Sent, stubbed } from "@/lib/testing";

import { RealBets } from "./real";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

beforeEach(() => {
  // 18:05 UTC on the game day: after the alert, before the 00:00 UTC start.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-17T18:05:00Z"));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const SKIPPED = {
  ...ALERT,
  game_id: 2026020251,
  home: "MTL",
  away: "OTT",
  status: "skipped: the stake is below the venue's minimum",
  stakes: [],
  max_stake: 0,
};

/** The stub, with real_entries taking inserts as the database would: one per bet. */
function board(refuse?: Answer) {
  const tables = { real_alerts: [ALERT, SKIPPED] as unknown[], real_entries: [] as unknown[] };
  const answer = (sent: Sent): Answer | undefined => {
    if (sent.method !== "POST" || !sent.url.pathname.endsWith("/real_entries")) return undefined;
    if (refuse) return refuse;
    return { status: 201, body: null };
  };
  const { supabase, requests } = stubbed(tables, new Set(["anon"]), answer);
  return { supabase, requests, tables };
}

test("an alerted bet shows its minimum price and the stake at the price typed", async () => {
  const { supabase } = board();
  render(<RealBets client={supabase} />);
  expect(await screen.findByText(/Practice: nothing here is a real bet/)).toBeTruthy();
  expect(screen.getByText(/CHI at TOR, starts .* · bet TOR/)).toBeTruthy();
  expect(screen.getByText(/No bet: the stake is below the venue's minimum/)).toBeTruthy();
  const price = screen.getByLabelText("Price");
  fireEvent.change(price, { target: { value: "2.10" } });
  expect(screen.getByRole("status").textContent).toBe("At 2.1 the stake is 0.90 €.");
  expect((screen.getByLabelText("Stake (€)") as HTMLInputElement).value).toBe("0.90");
  fireEvent.change(price, { target: { value: "2.00" } });
  expect(screen.getByRole("status").textContent).toBe("Below the minimum price 2.05: don't bet.");
});

test("a placed bet is sent as the migration takes it and shown once read back", async () => {
  const { supabase, requests, tables } = board();
  render(<RealBets client={supabase} />);
  fireEvent.change(await screen.findByLabelText("Price"), { target: { value: "2.10" } });
  fireEvent.change(screen.getByLabelText("Bet id"), { target: { value: " P1 " } });
  // The database answers the insert; the read after it finds the stored entry.
  tables.real_entries.push({
    game_date: ALERT.game_date,
    game_id: ALERT.game_id,
    outcome: "placed",
    matched_price: 2.1,
    stake: 0.9,
    bet_id: "P1",
    placed_utc: "2026-11-17T18:05:00Z",
    reason: null,
    entered_utc: "2026-11-17T18:05:30Z",
    late: false,
  });
  fireEvent.click(screen.getByText("Record placed bet"));
  expect(await screen.findByText(/Recorded: placed at 2.1, 0.90 €, bet id P1/)).toBeTruthy();
  const insert = requests.find((r) => r.method === "POST");
  expect(insert?.url.pathname).toBe("/rest/v1/real_entries");
});

test("the database's refusal is shown, and nothing is recorded", async () => {
  const { supabase } = board({
    status: 409,
    body: { code: "23505", message: "duplicate key value", details: null, hint: null },
  });
  render(<RealBets client={supabase} />);
  const select = (await screen.findAllByRole("combobox"))[0];
  fireEvent.change(select, { target: { value: "not matched" } });
  fireEvent.click(screen.getByText("Record not placed"));
  expect((await screen.findByRole("alert")).textContent).toBe(
    "This bet is already recorded: an entry is never changed.",
  );
});

test("a time at or after the start is refused before anything is sent", async () => {
  const { supabase, requests } = board();
  render(<RealBets client={supabase} />);
  fireEvent.change(await screen.findByLabelText("Price"), { target: { value: "2.10" } });
  fireEvent.change(screen.getByLabelText("Bet id"), { target: { value: "P1" } });
  const placed = screen.getByLabelText("Placed at (your time; empty for now)");
  const start = new Date(ALERT.start_utc);
  const local = new Date(start.getTime() - start.getTimezoneOffset() * 60_000);
  fireEvent.change(placed, { target: { value: local.toISOString().slice(0, 19) } });
  fireEvent.click(screen.getByText("Record placed bet"));
  expect((await screen.findByRole("alert")).textContent).toMatch(/at or after the game's start/);
  await waitFor(() => expect(requests.some((r) => r.method === "POST")).toBe(false));
});

test("before the migration the section says it isn't set up", async () => {
  const { supabase } = stubbed({}, new Set(["anon"]));
  render(<RealBets client={supabase} />);
  expect(await screen.findByText(/Not set up yet/)).toBeTruthy();
});

test("an empty placed time is the clock when the entry is sent, to the second", async () => {
  // The form mounts at 18:05:00 and the bet is recorded at 18:20:07 (Codex on #277).
  const { supabase, requests } = board();
  render(<RealBets client={supabase} />);
  fireEvent.change(await screen.findByLabelText("Price"), { target: { value: "2.10" } });
  fireEvent.change(screen.getByLabelText("Bet id"), { target: { value: " P1 " } });
  vi.setSystemTime(new Date("2026-11-17T18:20:07Z"));
  fireEvent.click(screen.getByText("Record placed bet"));
  await waitFor(() => expect(requests.some((r) => r.method === "POST")).toBe(true));
  const insert = requests.find((r) => r.method === "POST");
  expect(JSON.parse(insert?.body ?? "{}")).toEqual({
    game_date: ALERT.game_date,
    game_id: ALERT.game_id,
    outcome: "placed",
    matched_price: 2.1,
    stake: 0.9,
    bet_id: "P1",
    placed_utc: "2026-11-17T18:20:07.000Z",
  });
});
