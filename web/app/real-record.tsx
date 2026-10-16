"use client";

import type { SupabaseClient } from "@supabase/supabase-js";
import { useEffect, useState } from "react";

import { shortDate } from "@/lib/format";
import { loadRecord, type RealRecordState, stakeAt } from "@/lib/real";
import {
  priceGap,
  REAL_START,
  realBankroll,
  realRecord,
  STOP_DRAWDOWN,
  type Twin,
  twins,
  view,
} from "@/lib/real-record";
import type { Bet, LiveReport, RealStakes } from "@/lib/types";

import { LineChart } from "./charts";
import { Explain } from "./sections";

const euros = (x: number) => `${x.toFixed(2)} €`;
const signedEuros = (x: number) => `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(2)} €`;
const team = (bet: Bet) => (bet.side === "home" ? bet.home : bet.away);

/** The real record over time (#270, ADR 0039), apart from the paper one: the start rule until
 * real bets start, the real bankroll from 100 €, each paper bet's real twin and the price the
 * real bets got. Before the start it shows the practice record, labelled as such. */
export function RealRecord({
  client,
  bets,
  report,
}: {
  client: SupabaseClient;
  bets: Bet[];
  report: LiveReport | null;
}) {
  const [state, setState] = useState<RealRecordState | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    loadRecord(client).then(setState, (e: Error) => setError(e.message));
  }, [client]);

  if (error) return <p className="notice">Could not read the real record: {error}</p>;
  if (!state) return <p className="muted">Loading…</p>;
  if (!state.ready) return <p className="muted">Not set up yet (web/README.md).</p>;
  const shown = view(state.alerts);
  const settlements = state.settlements ?? [];
  const rows = twins(bets, state.alerts, state.entries, settlements, shown);
  const days = realBankroll(settlements, shown);
  const r = realRecord(settlements, shown);
  const gap = priceGap(rows);
  const stop = REAL_START * (1 - STOP_DRAWDOWN);
  return (
    <>
      <StartRule section={report?.real_stakes ?? null} started={shown === "real"} />
      {shown === "practice" ? (
        <p className="notice">
          Practice: nothing here is real. The practice record shows the chain working until real
          bets start; it never counts in the real bankroll.
        </p>
      ) : null}
      <p className="notice">
        Over this few bets, results are mostly luck, real ones as much as paper ones: CLV is the
        measure, and nothing in the policy changes because of them (ADR 0034).
      </p>
      <h3>{shown === "practice" ? "Practice bankroll" : "Real bankroll"}</h3>
      {state.settlements === null ? (
        <p className="muted">
          Not set up yet: apply supabase/migrations/20261016150000_real_settlements.sql for the
          bankroll (web/README.md).
        </p>
      ) : days.length === 0 ? (
        <p className="muted">No settled bet yet: the nightly run settles each night&apos;s games.</p>
      ) : (
        <>
          <p>
            {r.won + r.lost} settled bets: {r.won} won, {r.lost} lost
            {r.void ? `, ${r.void} void` : ""}. Profit {signedEuros(r.profit)} on{" "}
            {euros(r.staked)} staked; bankroll {euros(days[days.length - 1].balance)}.
          </p>
          <LineChart
            title={`${shown === "practice" ? "Practice" : "Real"} bankroll after each settled day, in euros`}
            points={days.map((d) => ({ label: shortDate(d.date), value: d.balance }))}
            format={(v) => v.toFixed(2)}
            reference={{
              values: days.map(() => stop),
              label: `${euros(stop)}: real bets stop`,
            }}
          />
        </>
      )}
      <h3>Each paper bet&apos;s {shown === "practice" ? "practice" : "real"} twin</h3>
      {rows.length === 0 ? (
        <p className="muted">No alert yet.</p>
      ) : (
        <Twins rows={rows} />
      )}
      <h3>The price against the paper price</h3>
      {gap.bets.length === 0 ? (
        <p className="muted">No placed bet yet.</p>
      ) : (
        <p>
          {gap.bets.length} placed: on average {signedPrice(gap.mean ?? 0)} on the paper price.
          Against the same stakes at the paper prices, the settled bets made{" "}
          {signedEuros(gap.profit)}.
        </p>
      )}
      <Explain terms={["realBankroll", "stopLine", "realTwin", "priceGap"]} />
    </>
  );
}

const signedPrice = (x: number) => `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(3)}`;

function Twins({ rows }: { rows: Twin[] }) {
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>Date</th>
            <th>Game</th>
            <th>Paper bet</th>
            <th>Real twin</th>
            <th className="number">Price difference</th>
            <th>Result</th>
            <th className="number">Profit</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((t) => (
            <tr key={`${t.bet.game_date}/${t.bet.game_id}`}>
              <td>{t.bet.game_date}</td>
              <td>
                {t.bet.away} at {t.bet.home}
              </td>
              <td>
                {team(t.bet)} @ {t.bet.price}
              </td>
              <td>{twinText(t)}</td>
              <td className="number">
                {t.state === "placed" && t.entry?.matched_price != null
                  ? signedPrice(t.entry.matched_price - t.bet.price)
                  : ""}
              </td>
              <td>{settlementText(t)}</td>
              <td className="number">{t.settlement ? signedEuros(t.settlement.profit) : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function twinText(t: Twin): string {
  switch (t.state) {
    case "placed": {
      const e = t.entry;
      const flags = [e?.late ? "recorded late" : "", outside(t) ? "outside the rule" : ""]
        .filter(Boolean)
        .join(", ");
      return `Placed at ${e?.matched_price}, ${euros(e?.stake ?? 0)}, bet id ${e?.bet_id}${flags ? ` (${flags})` : ""}`;
    }
    case "not placed":
      return `Not placed: ${t.entry?.reason}`;
    case "skipped":
      return `Skipped: ${t.alert?.status.replace(/^skipped: /, "")}`;
    case "not recorded":
      return "Not recorded";
    case "no alert":
      return "No alert that day";
  }
}

/** Whether a placed bet was outside the rule: a matched price under the alert's minimum, or a
 * stake above the alert's stake at that price (its own table, looked up). */
function outside(t: Twin): boolean {
  const e = t.entry;
  const a = t.alert;
  if (!e || !a || e.matched_price == null || e.stake == null) return false;
  return e.matched_price < a.min_price - 1e-9 || e.stake > stakeAt(a.stakes, e.matched_price) + 1e-9;
}

function settlementText(t: Twin): string {
  if (t.state !== "placed") return "";
  const s = t.settlement;
  if (!s) return "Awaiting the result";
  if (s.status === "void") return "Void";
  if (s.status === "cancelled") return "Cancelled by a correction";
  return s.won ? "Won" : "Lost";
}

/** The start rule's state from the latest report, until real bets start; then its start. */
function StartRule({ section, started }: { section: RealStakes | null; started: boolean }) {
  if (started) {
    return <p className="muted">Real bets have started (ADR 0039). The stops apply from then.</p>;
  }
  if (!section) {
    return <p className="muted">The start rule isn&apos;t in the latest report yet.</p>;
  }
  const yes = (holds: boolean) => (holds ? "holds" : "doesn't hold yet");
  return (
    <div className="start-rule">
      <p>
        <strong>The start rule</strong> ({section.window[0]} to {section.window[1]}, decided at
        Monday&apos;s report): {section.verdict}.
      </p>
      <ul>
        <li>
          CLV (a): {section.conditions.a.measured ? yes(section.conditions.a.holds) : "too few weeks to measure"}
        </li>
        <li>Operations (c): {yes(section.conditions.c.holds)}</li>
        <li>
          The venue (d): {section.conditions.d.holds ? `${section.conditions.d.venue}, ready` : "not chosen yet"}
        </li>
        <li>
          Your veto: {section.veto.applies ? "applies" : "lifted"} (paper bankroll{" "}
          {section.veto.paper_bankroll.toFixed(2)} against its {section.veto.start})
        </li>
        <li>The drawdown review: {section.drawdown_review.applies ? "open" : "not triggered"}</li>
      </ul>
    </div>
  );
}
