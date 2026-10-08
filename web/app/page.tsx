"use client";

import type { Session, SupabaseClient } from "@supabase/supabase-js";
import { type FormEvent, useEffect, useState } from "react";

import { easternDate } from "@/lib/format";
import { load, type Data } from "@/lib/load";
import { supabase } from "@/lib/supabase";

import { ClvHistory, Ledger, Report, Slate } from "./sections";

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
