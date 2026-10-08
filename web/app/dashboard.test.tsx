// @vitest-environment jsdom
// The page's states: not configured, signed out, signed in as an owner or not, a failed read,
// and a session that changes. Supabase is the real client over the PostgREST stub (lib/testing),
// with its auth calls replaced, so the board reads exactly as it would.
import type { AuthChangeEvent, Session, SupabaseClient } from "@supabase/supabase-js";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import report from "@/lib/fixtures/live-report.json";
import { type Answer, type Sent, type StoredReport, stubbed } from "@/lib/testing";

import { Dashboard, linkProblem, Signed } from "./dashboard";

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
      stake: 0.75,
      settlement: null,
      close_status: null,
      clv: null,
      fair_move: null,
    },
  ],
  live_reports: [stored],
};

/** The client, its auth replaced: `change` plays an auth event, as a magic link or a sign-out
 * would, and the spies record the auth calls. */
function client(start: Session | null, answer?: (sent: Sent) => Answer | undefined) {
  const { supabase, requests } = stubbed(TABLES, new Set([OWNER_TOKEN]), answer);
  let current = start;
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

test("a link that comes back signed out says why", async () => {
  const origin = "https://dashboard.example.com/";
  expect(linkProblem(origin)).toBeNull();
  // Used up before the click, as when a mail scanner opens it first.
  expect(
    linkProblem(
      `${origin}#error=access_denied&error_code=otp_expired&error_description=Email+link+is+invalid+or+has+expired`,
    ),
  ).toBe("The sign-in link didn't work: Email link is invalid or has expired.");
  expect(linkProblem(`${origin}?error_description=Email+link+is+invalid`)).toBe(
    "The sign-in link didn't work: Email link is invalid.",
  );
  // A code this browser can't complete: the link was asked for in another.
  expect(linkProblem(`${origin}?code=abc`)).toMatch(/in the browser that asked for it/);
  const { supabase } = client(null);
  render(<Signed client={supabase} problem={linkProblem(`${origin}?code=abc`)} />);
  await screen.findByText(/in the browser that asked for it/);
  expect(screen.getByPlaceholderText("password")).toBeTruthy();
});

test("an owner sees the slate, the bets, the CLV history and the report", async () => {
  const { supabase } = client(owner);
  render(<Signed client={supabase} />);
  expect(screen.getByText("Loading…")).toBeTruthy();
  await screen.findByText("Paper bets");
  expect(screen.getByText("Signed in as owner@example.com.", { exact: false })).toBeTruthy();
  // The slate's bet and the ledger's, at Pinnacle's price.
  expect(screen.getAllByText("WSH @ 1.900").length).toBe(1);
  expect(screen.getByText("awaiting the result")).toBeTruthy();
  // The report's own figures: CLV per bet with its interval, and its date in the history.
  expect(
    screen.getAllByText(/\+0\.0180 \[\+0\.0143, \+0\.0209\] \(66 bets, 5 weeks\)/).length,
  ).toBe(2);
  expect(screen.getByText(/Interim: no verdict/)).toBeTruthy();
  expect(screen.queryByText(/not a dashboard owner/)).toBeNull();
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
