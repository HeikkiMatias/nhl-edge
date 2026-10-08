"use client";

import type { Session, SupabaseClient } from "@supabase/supabase-js";
import { type FormEvent, useEffect, useState } from "react";

import { easternDate } from "@/lib/format";
import { supabase } from "@/lib/supabase";
import type { Bet, Figure, LiveReport, Prediction, ReportRow } from "@/lib/types";

import { ClvHistory, Ledger, Report, Slate } from "./sections";

const PREDICTION_COLUMNS =
  "game_date, game_id, start_utc, home, away, status, home_price, away_price, p_b1, p_b3, " +
  "p_blend, bet";
// Never won or profit: no result-based figure shows before the season's end (plan §11).
const BET_COLUMNS =
  "game_date, game_id, start_utc, home, away, side, price, p_side, ev, stake, settlement, " +
  "close_status, clv, fair_move";
// A day's slate is at most 16 games, so the latest 40 decisions hold the latest day's.
const LATEST_DECISIONS = 40;
const BETS_SHOWN = 200;
// The nightly reports' CLV per bet to date, newest first.
const CLV_HISTORY = 60;

type Data = {
  owner: boolean;
  day: string | null;
  slate: Prediction[];
  bets: Bet[];
  report: LiveReport | null;
  history: { as_of: string; clv: Figure }[];
};

export default function Dashboard() {
  const client = supabase();
  if (!client) {
    return (
      <main>
        <h1>NHL edge: paper trading</h1>
        <p className="notice">
          Not configured: set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY (see
          web/README.md).
        </p>
      </main>
    );
  }
  return <Signed client={client} />;
}

function Signed({ client }: { client: SupabaseClient }) {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    client.auth.getSession().then(({ data }) => {
      setSession(data.session);
      setReady(true);
    });
    const { data } = client.auth.onAuthStateChange((_event, next) => setSession(next));
    return () => data.subscription.unsubscribe();
  }, [client]);

  return (
    <main>
      <h1>NHL edge: paper trading</h1>
      {!ready ? (
        <p className="muted">Loading…</p>
      ) : session ? (
        <Board client={client} session={session} />
      ) : (
        <SignIn client={client} />
      )}
    </main>
  );
}

function SignIn({ client }: { client: SupabaseClient }) {
  const [email, setEmail] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  async function send(event: FormEvent) {
    event.preventDefault();
    const { error } = await client.auth.signInWithOtp({
      email,
      options: { emailRedirectTo: window.location.origin, shouldCreateUser: false },
    });
    setMessage(error ? error.message : "Check your email for the sign-in link.");
  }

  return (
    <>
      <p className="muted">Sign in with the email of a dashboard owner.</p>
      <form onSubmit={send}>
        <input
          type="email"
          required
          placeholder="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <button type="submit">Send link</button>
      </form>
      {message ? <p>{message}</p> : null}
    </>
  );
}

function Board({ client, session }: { client: SupabaseClient; session: Session }) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    load(client).then(setData, (e: Error) => setError(e.message));
  }, [client]);

  const signOut = (
    <p className="muted">
      Signed in as {session.user.email}.{" "}
      <button className="link" onClick={() => client.auth.signOut()}>
        Sign out
      </button>
    </p>
  );
  if (error) {
    return (
      <>
        {signOut}
        <p className="notice">Could not read the ledger: {error}</p>
      </>
    );
  }
  if (!data)
    return (
      <>
        {signOut}
        <p className="muted">Loading…</p>
      </>
    );
  if (!data.owner) {
    return (
      <>
        {signOut}
        <p className="notice">
          This account is not a dashboard owner, so row-level security shows it nothing. The owner
          adds it in Supabase&apos;s SQL editor:{" "}
          <code>
            insert into public.dashboard_owners (user_id) values (&apos;{session.user.id}&apos;);
          </code>
        </p>
      </>
    );
  }
  const today = easternDate(new Date());
  return (
    <>
      {signOut}
      <p className="notice">
        {data.report?.kind === "formal review"
          ? "The formal review (ADR 0032)."
          : `Interim: no verdict, promotion or real stake follows before the formal review on ${
              data.report?.review_date ?? "2027-04-12"
            } (ADR 0032). Results and profit show only at the season's end.`}
      </p>

      <h2>Slate</h2>
      {data.day !== today ? (
        <p className="muted">
          No decision for today ({today}) yet: a game day&apos;s are published after 12:45 ET.
        </p>
      ) : null}
      <Slate day={data.day} rows={data.slate} bets={data.bets} />

      <h2>Paper bets</h2>
      <Ledger bets={data.bets} />

      <h2>Cumulative CLV per bet</h2>
      <ClvHistory rows={data.history} />

      <h2>Live report</h2>
      <Report report={data.report} />
    </>
  );
}

async function load(client: SupabaseClient): Promise<Data> {
  const owner = await client.rpc("is_dashboard_owner");
  if (owner.error) throw owner.error;
  if (!owner.data)
    return { owner: false, day: null, slate: [], bets: [], report: null, history: [] };
  const [decisions, bets, reports, history] = await Promise.all([
    client
      .from("predictions")
      .select(PREDICTION_COLUMNS)
      .order("game_date", { ascending: false })
      .order("start_utc")
      .limit(LATEST_DECISIONS)
      .overrideTypes<Prediction[], { merge: false }>(),
    client
      .from("paper_bets")
      .select(BET_COLUMNS)
      .order("game_date", { ascending: false })
      .order("start_utc")
      .limit(BETS_SHOWN)
      .overrideTypes<Bet[], { merge: false }>(),
    client
      .from("live_reports")
      .select("as_of, kind, policy_version, report, code_version")
      .order("as_of", { ascending: false })
      .limit(1)
      .overrideTypes<ReportRow[], { merge: false }>(),
    client
      .from("live_reports")
      .select("as_of, clv:report->closing_value->clv_per_bet")
      .order("as_of", { ascending: false })
      .limit(CLV_HISTORY)
      .overrideTypes<{ as_of: string; clv: Figure }[], { merge: false }>(),
  ]);
  for (const result of [decisions, bets, reports, history]) if (result.error) throw result.error;
  const rows = decisions.data ?? [];
  const day = rows[0]?.game_date ?? null;
  return {
    owner: true,
    day,
    slate: rows.filter((r) => r.game_date === day),
    bets: bets.data ?? [],
    report: reports.data?.[0]?.report ?? null,
    history: history.data ?? [],
  };
}
