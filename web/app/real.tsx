"use client";

import type { SupabaseClient } from "@supabase/supabase-js";
import { type FormEvent, useCallback, useEffect, useState } from "react";

import { helsinkiTime } from "@/lib/format";
import {
  checkPlaced,
  loadReal,
  REASONS,
  type Reason,
  type RealState,
  recordNotPlaced,
  recordPlaced,
  stakeAt,
} from "@/lib/real";
import type { RealAlert, RealEntry } from "@/lib/types";

import { Explain } from "./sections";

const euros = (x: number) => `${x.toFixed(2)} €`;
const team = (a: RealAlert) => (a.side === "home" ? a.home : a.away);

/** A datetime-local input's value for an instant, in the browser's own time zone. */
function localValue(at: Date): string {
  const shifted = new Date(at.getTime() - at.getTimezoneOffset() * 60_000);
  return shifted.toISOString().slice(0, 16);
}

/** The latest alerted day's real bets (#268): each one's minimum price and stake at the price
 * offered, and the form that records what the owner did. */
export function RealBets({ client }: { client: SupabaseClient }) {
  const [state, setState] = useState<RealState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(
    () => loadReal(client).then(setState, (e: Error) => setError(e.message)),
    [client],
  );
  useEffect(() => {
    refresh();
  }, [refresh]);

  if (error) return <p className="notice">Could not read the real bets: {error}</p>;
  if (!state) return <p className="muted">Loading…</p>;
  if (!state.ready) {
    return (
      <p className="muted">
        Not set up yet: the real bets migration (supabase/migrations/20261016120000_real_bets.sql)
        isn&apos;t applied, or SUPABASE_REAL isn&apos;t set (web/README.md).
      </p>
    );
  }
  if (!state.day || !state.alerts.length) {
    return <p className="muted">No alert yet: the first comes after a 12:45 ET decision.</p>;
  }
  const mode = state.alerts[0].mode;
  return (
    <>
      <Mode mode={mode} day={state.day} bankroll={state.alerts[0].bankroll} />
      {state.alerts.map((alert) => (
        <RealBet
          key={alert.game_id}
          client={client}
          alert={alert}
          entry={state.entries[alert.game_id]}
          onRecorded={refresh}
        />
      ))}
      <Explain terms={["realStakes", "practice", "minPrice", "realStake", "realRecord"]} />
    </>
  );
}

function Mode({ mode, day, bankroll }: { mode: RealAlert["mode"]; day: string; bankroll: number }) {
  const label = {
    practice: "Practice: nothing here is a real bet. Try the form if you like; practice entries never count in the real bankroll.",
    real: "Real bets: place them at the venue, then record each one here.",
    paused: "Real bets are paused: a weekly report found too few closing prices. Nothing to place.",
    stopped: "Real bets are stopped. Restarting needs your decision; the paper ledger goes on.",
    closed: "Real bets didn't start by the end of November: a start needs your decision.",
  }[mode];
  return (
    <p className={mode === "real" ? "notice real" : "notice"}>
      <strong>{day}</strong> · {label} {mode === "real" || mode === "practice" ? `Bankroll ${euros(bankroll)}.` : ""}
    </p>
  );
}

function RealBet({
  client,
  alert,
  entry,
  onRecorded,
}: {
  client: SupabaseClient;
  alert: RealAlert;
  entry: RealEntry | undefined;
  onRecorded: () => void;
}) {
  const title = `${alert.away} at ${alert.home}, starts ${helsinkiTime(alert.start_utc, alert.game_date)} · bet ${team(alert)}`;
  if (alert.status !== "alerted") {
    return (
      <div className="real-bet">
        <h3>{title}</h3>
        <p className="muted">No bet: {alert.status.replace(/^skipped: /, "")}.</p>
      </div>
    );
  }
  return (
    <div className="real-bet">
      <h3>{title}</h3>
      <p>
        Minimum price <strong>{alert.min_price}</strong>. Stake {euros(alert.max_stake)} at{" "}
        {alert.paper_price} or more.
      </p>
      {entry ? <Recorded entry={entry} /> : <EntryForm client={client} alert={alert} onRecorded={onRecorded} />}
    </div>
  );
}

function Recorded({ entry }: { entry: RealEntry }) {
  const late = entry.late ? " Recorded after the start (late)." : "";
  if (entry.outcome === "not placed") {
    return <p className="recorded">Recorded: not placed ({entry.reason}).{late}</p>;
  }
  return (
    <p className="recorded">
      Recorded: placed at {entry.matched_price}, {euros(entry.stake ?? 0)}, bet id {entry.bet_id},{" "}
      {entry.placed_utc ? helsinkiTime(entry.placed_utc) : ""}.{late}
    </p>
  );
}

function EntryForm({
  client,
  alert,
  onRecorded,
}: {
  client: SupabaseClient;
  alert: RealAlert;
  onRecorded: () => void;
}) {
  const [price, setPrice] = useState("");
  const [stake, setStake] = useState("");
  const [stakeEdited, setStakeEdited] = useState(false);
  const [betId, setBetId] = useState("");
  const [placedAt, setPlacedAt] = useState(() => localValue(new Date()));
  const [reason, setReason] = useState<Reason>(REASONS[0]);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const offered = Number(price);
  const allowed = price && offered > 1 ? stakeAt(alert.stakes, offered) : null;
  const bet = { price: offered, stake: Number(stake), betId, placedAt: new Date(placedAt) };
  const { warnings } = checkPlaced(alert, bet, new Date());

  function onPrice(value: string) {
    setPrice(value);
    const at = Number(value);
    if (!stakeEdited && at > 1) {
      const rule = stakeAt(alert.stakes, at);
      setStake(rule > 0 ? rule.toFixed(2) : "");
    }
  }

  async function place(event: FormEvent) {
    event.preventDefault();
    const checked = checkPlaced(alert, bet, new Date());
    setErrors(checked.errors);
    if (checked.errors.length) return;
    setBusy(true);
    const refused = await recordPlaced(client, alert, bet);
    setBusy(false);
    if (refused) setErrors([refused]);
    else onRecorded();
  }

  async function notPlaced(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    const refused = await recordNotPlaced(client, alert, reason);
    setBusy(false);
    if (refused) setErrors([refused]);
    else onRecorded();
  }

  return (
    <>
      <form onSubmit={place} aria-label={`Record ${team(alert)}`}>
        <label>
          Price
          <input
            type="number"
            inputMode="decimal"
            step="0.001"
            min="1.001"
            required
            value={price}
            onChange={(e) => onPrice(e.target.value)}
          />
        </label>
        <label>
          Stake (€)
          <input
            type="number"
            inputMode="decimal"
            step="0.01"
            min="0.01"
            required
            value={stake}
            onChange={(e) => {
              setStake(e.target.value);
              setStakeEdited(true);
            }}
          />
        </label>
        <label>
          Bet id
          <input required value={betId} onChange={(e) => setBetId(e.target.value)} />
        </label>
        <label>
          Placed at (your time)
          <input
            type="datetime-local"
            required
            value={placedAt}
            onChange={(e) => setPlacedAt(e.target.value)}
          />
        </label>
        <button type="submit" disabled={busy}>
          Record placed bet
        </button>
      </form>
      {allowed !== null ? (
        <p className="muted" role="status">
          {allowed > 0
            ? `At ${offered} the stake is ${euros(allowed)}.`
            : `Below the minimum price ${alert.min_price}: don't bet.`}
        </p>
      ) : null}
      {warnings.map((w) => (
        <p key={w} className="warning">
          {w}
        </p>
      ))}
      <form onSubmit={notPlaced} aria-label={`Not placed ${team(alert)}`}>
        <select value={reason} onChange={(e) => setReason(e.target.value as Reason)}>
          {REASONS.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <button type="submit" className="secondary" disabled={busy}>
          Record not placed
        </button>
      </form>
      {errors.map((e) => (
        <p key={e} className="error" role="alert">
          {e}
        </p>
      ))}
    </>
  );
}
