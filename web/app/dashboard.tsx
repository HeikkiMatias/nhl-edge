"use client";

import type { Session, SupabaseClient } from "@supabase/supabase-js";
import { type FormEvent, useCallback, useEffect, useState } from "react";

import { easternDate, easternInstant, helsinkiTime } from "@/lib/format";
import { load, type Data } from "@/lib/load";
import type { RealState } from "@/lib/real";
import { supabase } from "@/lib/supabase";

import { HELP } from "@/lib/help";

import { LiveScores } from "./live";
import { MarketResults } from "./market";
import { MarketToday } from "./market-today";
import { RealBets } from "./real";
import { RealRecord } from "./real-record";
import { ClvHistory, Glossary, Headline, Ledger, Report, Results, Slate } from "./sections";
import { Card, Chip, Panel, Tabs, ThemeToggle, useHash } from "./ui";

// The tabs (#398): market making first, the direction the owner follows most; then the bets,
// the models and the help. Each is linkable by the URL's hash, such as #betting/results.
const TABS = [
  { id: "market", label: "Market making" },
  { id: "betting", label: "Betting" },
  { id: "models", label: "Models" },
  { id: "help", label: "Help" },
];
const BETTING = [
  { id: "today", label: "Today" },
  { id: "results", label: "Results" },
];
// Market making's sub-tabs share the ids, so their panels' ids are prefixed.
const MARKET = [
  { id: "market-today", label: "Today" },
  { id: "market-results", label: "Results" },
];

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
      <header className="topbar">
        <h1>NHL edge</h1>
        <ThemeToggle />
      </header>
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
  // Bumped when the form records a bet, so the real record reads it at once.
  const [recorded, setRecorded] = useState(0);
  const onRecorded = useCallback(() => setRecorded((n) => n + 1), []);
  // The real bets' day as the form last read it, for the live games' recorded bets.
  const [real, setReal] = useState<RealState | null>(null);
  const [path, go] = useHash();
  const tab = TABS.some((t) => t.id === path[0]) ? path[0] : "market";
  const sub = (tab === "betting" || tab === "market") && path[1] === "results" ? "results" : "today";
  // A tab is drawn when first opened and kept after, so moving back is instant and a form keeps
  // what was typed in it.
  const [opened, setOpened] = useState<Set<string>>(() => new Set());
  const here = tab === "betting" || tab === "market" ? `${tab}/${sub}` : tab;
  if (!opened.has(here)) setOpened(new Set(opened).add(here));

  useEffect(() => {
    load(client).then(setData, (e: Error) => setError(e.message));
  }, [client]);

  const signOut = (
    <p className="muted account">
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
  const review = data.report?.review_date ?? "2027-04-12";
  return (
    <>
      {signOut}
      <p className="status-line">
        <Chip tone="info">
          {data.report?.kind === "formal review" ? "Formal review" : `Interim · review ${review}`}
        </Chip>
      </p>
      <Tabs tabs={TABS} active={tab} onSelect={(id) => go([id])} label="Dashboard" />

      {opened.has("market/today") || opened.has("market/results") ? (
        <div hidden={tab !== "market"}>
          <Panel id="market">
            <Tabs
              tabs={MARKET}
              active={`market-${sub}`}
              onSelect={(id) => go(["market", id.replace("market-", "")])}
              label="Market making"
              small
            />
            {opened.has("market/today") ? (
              <div
                hidden={sub !== "today"}
                role="tabpanel"
                id="panel-market-today"
                aria-labelledby="tab-market-today"
              >
                <MarketToday client={client} />
              </div>
            ) : null}
            {opened.has("market/results") ? (
              <div
                hidden={sub !== "results"}
                role="tabpanel"
                id="panel-market-results"
                aria-labelledby="tab-market-results"
              >
                <MarketResults client={client} />
              </div>
            ) : null}
          </Panel>
        </div>
      ) : null}

      {opened.has("betting/today") || opened.has("betting/results") ? (
        <div hidden={tab !== "betting"}>
          <Panel id="betting">
            <Tabs
              tabs={BETTING}
              active={sub}
              onSelect={(id) => go(["betting", id])}
              label="Betting"
              small
            />
            {opened.has("betting/today") ? (
              <div hidden={sub !== "today"} role="tabpanel" id="panel-today" aria-labelledby="tab-today">
                {/* Where the day's Telegram alert links to (#268). */}
                <Card title="Real bets" topic="realBets" wide>
                  <RealBets client={client} onRecorded={onRecorded} onState={setReal} />
                </Card>
                <Card title="Live games and our bets" topic="liveScores" wide>
                  <LiveScores day={data.day} rows={data.slate} bets={data.bets} real={real} />
                </Card>
                <Card title="Slate" topic="slate" wide>
                  {data.day !== today ? (
                    <p className="muted">
                      No decision for today&apos;s game day ({today}) yet: it is published after{" "}
                      {helsinkiTime(easternInstant(today, "12:45").toISOString())} Helsinki time
                      (12:45 in New York).
                    </p>
                  ) : null}
                  <Slate day={data.day} rows={data.slate} bets={data.bets} />
                </Card>
              </div>
            ) : null}
            {opened.has("betting/results") ? (
              <div
                hidden={sub !== "results"}
                role="tabpanel"
                id="panel-results"
                aria-labelledby="tab-results"
              >
                <Card title="Paper trading" topic="paper" wide>
                  <p className="notice">
                    {data.report?.kind === "formal review"
                      ? "The formal review (ADR 0032)."
                      : `Interim: no verdict or promotion follows before the formal review on ${review} (ADR 0032), and real stakes only by ADR 0039's start rule. The results below are mostly luck over this few bets: CLV is the measure (ADR 0034).`}
                  </p>
                  <Headline bets={data.bets} report={data.report} />
                </Card>
                <Card title="Real record" topic="realRecord" wide>
                  <RealRecord
                    client={client}
                    bets={data.bets}
                    report={data.report}
                    version={recorded}
                  />
                </Card>
                <Card title="Results and bankroll" topic="results" wide>
                  <Results bets={data.bets} />
                </Card>
                <Card title="Paper bets" topic="ledger" wide>
                  <Ledger bets={data.bets} scores={data.scores} />
                </Card>
                <Card title="Cumulative CLV per bet" topic="clv" wide>
                  <ClvHistory rows={data.history} />
                </Card>
              </div>
            ) : null}
          </Panel>
        </div>
      ) : null}

      {opened.has("models") ? (
        <div hidden={tab !== "models"}>
          <Panel id="models">
            <Card title="Live report" topic="report" wide>
              <Report report={data.report} />
            </Card>
          </Panel>
        </div>
      ) : null}

      {opened.has("help") ? (
        <div hidden={tab !== "help"}>
          <Panel id="help">
            <HelpTab />
          </Panel>
        </div>
      ) : null}
    </>
  );
}

/** The daily routine, what each section shows, and every term. */
function HelpTab() {
  return (
    <>
      <Card title="The daily routine" wide>
        <ol className="routine">
          <li>
            <strong>Midday (12:45 New York, 19:45 Helsinki):</strong> the decision runs and the
            Telegram alert lists the day&apos;s bets with their lowest prices and stakes. Its link
            opens Betting › Today.
          </li>
          <li>
            <strong>Before each game:</strong> place the bet at the venue, then record it under
            Real bets: placed, with the price and stake you got, or not placed and why.
          </li>
          <li>
            <strong>Overnight:</strong> the nightly run settles the games, writes the live report
            and copies the market-making simulator&apos;s day. Nothing to do.
          </li>
          <li>
            <strong>Monday:</strong> the weekly report, and the check of the real record against
            the venue&apos;s statement.
          </li>
        </ol>
      </Card>
      <Card title="What each section shows" wide>
        <dl className="help-list">
          {Object.values(HELP).map((h) => (
            <div key={h.title}>
              <dt>{h.title}</dt>
              <dd>
                {h.what} <span className="muted">{h.read}</span>
              </dd>
            </div>
          ))}
        </dl>
      </Card>
      <Card title="What everything means" wide>
        <Glossary />
      </Card>
    </>
  );
}
