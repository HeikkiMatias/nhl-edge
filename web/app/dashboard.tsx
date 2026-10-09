"use client";

import type { Session, SupabaseClient } from "@supabase/supabase-js";
import { type FormEvent, useEffect, useState } from "react";

import { easternDate, easternInstant, helsinkiTime } from "@/lib/format";
import { load, type Data } from "@/lib/load";
import { supabase } from "@/lib/supabase";

import { LiveScores } from "./live";
import { ClvHistory, Glossary, Headline, Ledger, Report, Results, Slate } from "./sections";

/** The page: the sign-in form, or the board for a signed-in user, read through the client made
 * from the public URL and anon key (lib/supabase.ts). */
export function Dashboard() {
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

/** The signed-in state, followed through Supabase's auth events. An email link that came back
 * signed out says why, in Supabase's own words: the client's initialization reports the link's
 * error, an expired or used-up link (as when a mail scanner opened it first) or a code it couldn't
 * exchange. A session clears it. */
export function Signed({ client }: { client: SupabaseClient }) {
  const [session, setSession] = useState<Session | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    Promise.all([client.auth.initialize(), client.auth.getSession()]).then(
      ([{ error }, { data }]) => {
        setSession(data.session);
        setProblem(error && !data.session ? `The sign-in link didn't work: ${error.message}` : null);
        setReady(true);
      },
    );
    const { data } = client.auth.onAuthStateChange((_event, next) => {
      setSession(next);
      if (next) setProblem(null);
    });
    return () => data.subscription.unsubscribe();
  }, [client]);

  return (
    <main>
      <h1>NHL edge: paper trading</h1>
      {!ready ? (
        <p className="muted">Loading…</p>
      ) : session ? (
        // Keyed by the user, so a session replaced by another account's starts a fresh board:
        // no account ever sees the rows, or the denial, loaded for another.
        <Board key={session.user.id} client={client} session={session} />
      ) : (
        <SignIn client={client} problem={problem} />
      )}
    </main>
  );
}

function SignIn({ client, problem }: { client: SupabaseClient; problem: string | null }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState<string | null>(problem);

  async function signIn(event: FormEvent) {
    event.preventDefault();
    const { error } = await client.auth.signInWithPassword({ email, password });
    // A session arrives through the auth events; only a refusal needs saying.
    setMessage(error ? error.message : null);
  }

  async function sendLink() {
    if (!email) {
      setMessage("Enter your email first.");
      return;
    }
    const { error } = await client.auth.signInWithOtp({
      email,
      options: { emailRedirectTo: window.location.origin, shouldCreateUser: false },
    });
    setMessage(error ? error.message : "Check your email for the sign-in link.");
  }

  return (
    <>
      <p className="muted">
        Sign in as a dashboard owner: the email and password of your Supabase user, or an email
        link.
      </p>
      <form onSubmit={signIn}>
        <input
          type="email"
          required
          placeholder="email"
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <input
          type="password"
          required
          placeholder="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <button type="submit">Sign in</button>
        <button type="button" className="link" onClick={sendLink}>
          Email me a link instead
        </button>
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
            } (ADR 0032). The results below are mostly luck over this few bets: CLV is the measure (ADR 0034).`}
      </p>
      <Headline bets={data.bets} report={data.report} />

      <h2>Live scores</h2>
      <LiveScores day={data.day} rows={data.slate} bets={data.bets} />

      <h2>Slate</h2>
      {data.day !== today ? (
        <p className="muted">
          No decision for today&apos;s game day ({today}) yet: it is published after{" "}
          {helsinkiTime(easternInstant(today, "12:45").toISOString())} Helsinki time (12:45 in New
          York).
        </p>
      ) : null}
      <Slate day={data.day} rows={data.slate} bets={data.bets} />

      <h2>Results and bankroll</h2>
      <Results bets={data.bets} />

      <h2>Paper bets</h2>
      <Ledger bets={data.bets} scores={data.scores} />

      <h2>Cumulative CLV per bet</h2>
      <ClvHistory rows={data.history} />

      <h2>Live report</h2>
      <Report report={data.report} />

      <h2>What everything means</h2>
      <Glossary />
    </>
  );
}
