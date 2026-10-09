// @vitest-environment jsdom
// The page's states: not configured, signed out, signed in as an owner or not, a failed read,
// and a session that changes. Supabase is the real client over the PostgREST stub (lib/testing),
// with its auth calls replaced, so the board reads exactly as it would.
import type { AuthChangeEvent, Session, SupabaseClient } from "@supabase/supabase-js";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import report from "@/lib/fixtures/live-report.json";
import { type Answer, type Sent, type StoredReport, stubbed } from "@/lib/testing";

import { Dashboard, Signed } from "./dashboard";

// The auth events below are played inside act(), as React expects of a test environment.
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(cleanup);

const OWNER_TOKEN = "owner-token";

function session(id: string, email: string, token: string): Session {
  return {
    access_token: token,
    refresh_token: "refresh",
    expires_in: 3600,
    expires_at: 4_102_444_800,
    token_type: "bearer",
    user: { id, email, aud: "authenticated", app_metadata: {}, user_metadata: {}, created_at: "" },
  };
}

const owner = session("11111111-1111-1111-1111-111111111111", "owner@example.com", OWNER_TOKEN);
const stranger = session("22222222-2222-2222-2222-222222222222", "other@example.com", "other");

const stored: StoredReport = {
  as_of: report.as_of,
  kind: report.kind,
  policy_version: report.policy_version,
  code_version: "live-report-20261115-abc1234",
  report,
};

const TABLES = {
  predictions: [
    {
      game_date: "2026-10-08",
      game_id: 2026020011,
      start_utc: "2026-10-08T23:00:00+00:00",
      home: "WSH",
      away: "PIT",
      status: "predicted",
      home_price: 1.9,
      away_price: 2.0,
      p_b1: 0.5,
      p_b3: 0.53,
      p_blend: 0.52,
      bet: true,
    },
  ],
  paper_bets: [
    {
      game_date: "2026-10-08",
      game_id: 2026020011,
      start_utc: "2026-10-08T23:00:00+00:00",
      home: "WSH",
      away: "PIT",
      side: "home",
      price: 1.9,
      p_side: 0.52,
      ev: 0.044,
      bankroll: 100,
      stake: 0.75,
      settled_utc: null,
      settlement: null,
      won: null,
      profit: null,
      close_status: null,
      clv: null,
      fair_move: null,
    },
    {
      game_date: "2026-10-07",
      game_id: 2026020003,
      start_utc: "2026-10-07T23:00:00+00:00",
      home: "BUF",
      away: "DAL",
      side: "away",
      price: 1.88,
      p_side: 0.58,
      ev: 0.09,
      bankroll: 100,
      stake: 1.09,
      settled_utc: "2026-10-08T05:01:12+00:00",
      settlement: "settled",
      won: true,
      profit: 0.9592,
      close_status: "proxy",
      clv: 0.031,
      fair_move: 0.012,
    },
  ],
  live_reports: [stored],
  games: [
    { game_id: 2026020003, game_date: "2026-10-07", home_score: 2, away_score: 4, decided_in: "REG" },
  ],
};

/** The client, its auth replaced: `change` plays an auth event, as a magic link or a sign-out
 * would, and the spies record the auth calls. */
function client(
  start: Session | null,
  answer?: (sent: Sent) => Answer | undefined,
  linkError: string | null = null,
) {
  const { supabase, requests } = stubbed(TABLES, new Set([OWNER_TOKEN]), answer);
  let current = start;
  // What the client's initialization reports of the URL it was opened at: an email link's error.
  vi.spyOn(supabase.auth, "initialize").mockResolvedValue({
    error: linkError ? ({ message: linkError } as never) : null,
  });
  let listener: ((event: AuthChangeEvent, next: Session | null) => void) | undefined;
  vi.spyOn(supabase.auth, "getSession").mockImplementation(async () =>
    current
      ? { data: { session: current }, error: null }
      : { data: { session: null }, error: null },
  );
  vi.spyOn(supabase.auth, "onAuthStateChange").mockImplementation((callback) => {
    listener = callback as typeof listener;
    return { data: { subscription: { id: "test", callback, unsubscribe: () => {} } } };
  });
  const change = (event: AuthChangeEvent, next: Session | null) =>
    act(() => {
      current = next;
      listener?.(event, next);
    });
  const signIn = vi
    .spyOn(supabase.auth, "signInWithOtp")
    .mockResolvedValue({ data: { user: null, session: null }, error: null });
  // A right password signs in as the owner; any other is refused, as Supabase refuses it.
  const signInWithPassword = vi
    .spyOn(supabase.auth, "signInWithPassword")
    .mockImplementation(async (credentials) => {
      if ("email" in credentials && credentials.password === "right") {
        await change("SIGNED_IN", owner);
        return { data: { user: owner.user, session: owner }, error: null } as never;
      }
      return {
        data: { user: null, session: null },
        error: { message: "Invalid login credentials" },
      } as never;
    });
  const signOut = vi.spyOn(supabase.auth, "signOut").mockImplementation(async () => {
    await change("SIGNED_OUT", null);
    return { error: null };
  });
  return {
    supabase: supabase as SupabaseClient,
    requests,
    change,
    signIn,
    signInWithPassword,
    signOut,
  };
}

test("without the public URL and key the page says it isn't configured", () => {
  render(<Dashboard />);
  expect(screen.getByText(/Not configured/)).toBeTruthy();
  expect(screen.queryByRole("button")).toBeNull();
});

test("signed out, the right password opens the owner's board", async () => {
  const { supabase, signInWithPassword } = client(null);
  render(<Signed client={supabase} />);
  const email = await screen.findByPlaceholderText("email");
  fireEvent.change(email, { target: { value: "owner@example.com" } });
  fireEvent.change(screen.getByPlaceholderText("password"), { target: { value: "right" } });
  fireEvent.submit(email.closest("form") as HTMLFormElement);
  await screen.findByText("Paper bets");
  expect(signInWithPassword).toHaveBeenCalledWith({
    email: "owner@example.com",
    password: "right",
  });
});

test("a wrong password shows Supabase's reason and stays signed out", async () => {
  const { supabase } = client(null);
  render(<Signed client={supabase} />);
  const email = await screen.findByPlaceholderText("email");
  fireEvent.change(email, { target: { value: "owner@example.com" } });
  fireEvent.change(screen.getByPlaceholderText("password"), { target: { value: "wrong" } });
  fireEvent.submit(email.closest("form") as HTMLFormElement);
  await screen.findByText("Invalid login credentials");
  expect(screen.queryByText("Paper bets")).toBeNull();
});

test("signed out, the form sends an email link to existing users only", async () => {
  const { supabase, signIn } = client(null);
  render(<Signed client={supabase} />);
  const email = await screen.findByPlaceholderText("email");
  fireEvent.click(screen.getByRole("button", { name: "Email me a link instead" }));
  await screen.findByText("Enter your email first.");
  expect(signIn).not.toHaveBeenCalled();
  fireEvent.change(email, { target: { value: "owner@example.com" } });
  fireEvent.click(screen.getByRole("button", { name: "Email me a link instead" }));
  await screen.findByText("Check your email for the sign-in link.");
  expect(signIn).toHaveBeenCalledWith({
    email: "owner@example.com",
    options: { emailRedirectTo: window.location.origin, shouldCreateUser: false },
  });
});

test("a refused email link shows Supabase's reason", async () => {
  const { supabase, signIn } = client(null);
  signIn.mockResolvedValueOnce({
    data: { user: null, session: null },
    error: { message: "Signups not allowed for otp" } as never,
  });
  render(<Signed client={supabase} />);
  const email = await screen.findByPlaceholderText("email");
  fireEvent.change(email, { target: { value: "someone@example.com" } });
  fireEvent.click(screen.getByRole("button", { name: "Email me a link instead" }));
  await screen.findByText("Signups not allowed for otp");
});

test("a link that comes back signed out says why, until a sign-in clears it", async () => {
  // Codex on #206: Supabase's own reason, here a link a mail scanner used up first.
  const reason = "Email link is invalid or has expired";
  const { supabase, signOut } = client(null, undefined, reason);
  render(<Signed client={supabase} />);
  await screen.findByText(`The sign-in link didn't work: ${reason}`);
  expect(screen.getByPlaceholderText("password")).toBeTruthy();
  const email = screen.getByPlaceholderText("email");
  fireEvent.change(email, { target: { value: "owner@example.com" } });
  fireEvent.change(screen.getByPlaceholderText("password"), { target: { value: "right" } });
  fireEvent.submit(email.closest("form") as HTMLFormElement);
  await screen.findByText("Paper bets");
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  await screen.findByPlaceholderText("email");
  expect(signOut).toHaveBeenCalledOnce();
  expect(screen.queryByText(/didn't work/)).toBeNull();
});

test("a link that signed in leaves no message after signing out", async () => {
  // The error is the client's, not the URL's: a link that worked reports none.
  const { supabase } = client(owner);
  render(<Signed client={supabase} />);
  await screen.findByText("Paper bets");
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  await screen.findByPlaceholderText("email");
  expect(screen.queryByText(/didn't work/)).toBeNull();
});

test("an owner sees the slate, the bets, the CLV history and the report", async () => {
  const { supabase } = client(owner);
  render(<Signed client={supabase} />);
  expect(screen.getByText("Loading…")).toBeTruthy();
  await screen.findByText("Paper bets");
  expect(screen.getByText("Signed in as owner@example.com.", { exact: false })).toBeTruthy();
  // The slate's bet and the ledger's, at Pinnacle's price.
  expect(screen.getAllByText("WSH @ 1.900").length).toBe(1);
  expect(screen.getByText("Awaiting the result")).toBeTruthy();
  // The report's own figures: CLV per bet with its interval in the headline tile, the report and
  // its date in the history.
  expect(
    screen.getAllByText(/\+0\.0180 \[\+0\.0143, \+0\.0209\] \(66 bets, 5 weeks\)/).length,
  ).toBe(3);
  expect(screen.getByText(/Interim: no verdict/)).toBeTruthy();
  expect(screen.queryByText(/not a dashboard owner/)).toBeNull();
});

test("an owner sees each bet's result, its score and the bankroll, with CLV first", async () => {
  // ADR 0034: results show during the season, beside the note that they are mostly luck.
  const { supabase } = client(owner);
  render(<Signed client={supabase} />);
  await screen.findByText("Results and bankroll");
  const won = screen.getByText("Won");
  expect(won.className).toBe("won");
  expect(screen.getByText("DAL 4–2 BUF")).toBeTruthy();
  expect(screen.getAllByText("+0.96").length).toBeGreaterThan(0);
  expect(screen.getByText("Awaiting the result")).toBeTruthy();
  // The headline tiles lead with CLV, the measure; the bankroll is 100 plus the profit.
  const tiles = [...document.querySelectorAll(".tile .label")].map((t) => t.textContent);
  expect(tiles[0]).toBe("CLV per bet, the measure");
  expect(screen.getAllByText("100.96").length).toBe(2); // the tile, and the day in its table
  expect(screen.getByText(/Over this few bets, results are mostly luck/)).toBeTruthy();
  // Far from the review line, which is named in a caption rather than drawn.
  expect(screen.getByText(/is off this scale: the deepest drawdown so far is 0.0%/)).toBeTruthy();
  // The charts, each named for a screen reader.
  expect(screen.getByRole("img", { name: /Paper bankroll after each settled day/ })).toBeTruthy();
  expect(screen.getByRole("img", { name: "Paper bets per game day" })).toBeTruthy();
  expect(screen.getByRole("img", { name: /expected return at the decision against its CLV/ })).toBeTruthy();
  // Every term explained.
  expect(screen.getByText("What everything means")).toBeTruthy();
  expect(screen.getAllByText("CLV (closing line value)").length).toBeGreaterThan(1);
});

test("the bankroll chart's readout follows the keys", async () => {
  const { supabase } = client(owner);
  render(<Signed client={supabase} />);
  const chart = await screen.findByRole("img", { name: /Paper bankroll after each settled day/ });
  fireEvent.focus(chart);
  const readout = await screen.findByRole("status");
  expect(readout.textContent).toContain("101.0");
  expect(readout.textContent).toContain("Oct 7");
});

test("before the games migration, results show and say why the scores don't", async () => {
  const { supabase } = client(owner, (sent) =>
    sent.url.pathname.endsWith("/games")
      ? { status: 401, body: { code: "42501", message: "permission denied for table games" } }
      : undefined,
  );
  render(<Signed client={supabase} />);
  await screen.findByText("Won");
  expect(screen.queryByText("DAL 4–2 BUF")).toBeNull();
  expect(screen.getByText(/Scores show once the owner applies the migration/)).toBeTruthy();
});

test("an account that isn't an owner sees nothing but its user id", async () => {
  const { supabase, requests } = client(stranger);
  render(<Signed client={supabase} />);
  await screen.findByText(/not a dashboard owner/);
  expect(screen.getByText(new RegExp(stranger.user.id))).toBeTruthy();
  expect(screen.queryByText("Paper bets")).toBeNull();
  expect(requests.map((r) => r.url.pathname)).toEqual(["/rest/v1/rpc/is_dashboard_owner"]);
});

test("a failed read shows the error, not an empty board", async () => {
  const { supabase } = client(owner, (sent) =>
    sent.url.pathname.endsWith("/paper_bets")
      ? { status: 401, body: { code: "PGRST301", message: "JWT expired" } }
      : undefined,
  );
  render(<Signed client={supabase} />);
  await screen.findByText("Could not read the ledger: JWT expired");
  expect(screen.queryByText("Paper bets")).toBeNull();
});

test("a session replaced by another account's reads the board again", async () => {
  // Codex on #197: no account may see the rows, or the denial, loaded for another.
  const { supabase, change } = client(owner);
  render(<Signed client={supabase} />);
  await screen.findByText("Paper bets");
  await change("SIGNED_IN", stranger);
  await screen.findByText(/not a dashboard owner/);
  expect(screen.queryByText("Paper bets")).toBeNull();
  expect(screen.queryByText("WSH @ 1.900")).toBeNull();
  await change("SIGNED_IN", owner);
  await screen.findByText("Paper bets");
  expect(screen.queryByText(/not a dashboard owner/)).toBeNull();
});

test("signing out returns to the form", async () => {
  const { supabase, signOut } = client(owner);
  render(<Signed client={supabase} />);
  await screen.findByText("Paper bets");
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
  await screen.findByPlaceholderText("email");
  expect(signOut).toHaveBeenCalledOnce();
  await waitFor(() => expect(screen.queryByText("Paper bets")).toBeNull());
});
