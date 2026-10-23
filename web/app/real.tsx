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
import type { RealAlert, RealEntry, RealOrder } from "@/lib/types";

import { Explain } from "./sections";

const euros = (x: number) => `${x.toFixed(2)} €`;
const team = (a: RealAlert) => (a.side === "home" ? a.home : a.away);

/** The latest alerted day's real bets (#268): each one's minimum price and stake at the price
 * offered, and the form that records what the owner did. */
export function RealBets({
  client,
  onRecorded,
}: {
  client: SupabaseClient;
  /** Told when an entry is recorded, so the real record beside it reads it too (#270). */
  onRecorded?: () => void;
}) {
  const [state, setState] = useState<RealState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(
    () => loadReal(client).then(setState, (e: Error) => setError(e.message)),
    [client],
  );
  const refresh = useCallback(() => {
    load();
    onRecorded?.();
  }, [load, onRecorded]);
  useEffect(() => {
    load();
  }, [load]);
  // The clock the bets are shown against, read again every 30 seconds: a page left open past a
  // game's start stops offering its bet (Codex on #277).
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, []);

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
  const automatic = state.alerts.some((a) => a.exchange);
  return (
    <>
      <Mode
        mode={mode}
        day={state.day}
        bankroll={state.alerts[0].bankroll}
        automatic={automatic}
      />
      {state.alerts.map((alert) => (
        <RealBet
          key={alert.game_id}
          client={client}
          alert={alert}
          entry={state.entries[alert.game_id]}
          onRecorded={refresh}
          now={now}
        />
      ))}
      <Explain terms={["realStakes", "practice", "minPrice", "realStake", "realRecord"]} />
    </>
  );
}

function Mode({
  mode,
  day,
  bankroll,
  automatic,
}: {
  mode: RealAlert["mode"];
  day: string;
  bankroll: number;
  automatic: boolean;
}) {
  const label = {
    practice: "Practice: nothing here is a real bet. Try the form if you like; practice entries never count in the real bankroll.",
    real: automatic
      ? "Real bets: placed automatically on Kalshi, each one recorded here from its fills."
      : "Real bets: place them at the venue, then record each one here.",
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
  now,
}: {
  client: SupabaseClient;
  alert: RealAlert;
  entry: RealEntry | undefined;
  onRecorded: () => void;
  now: number;
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
  if (entry) {
    return (
      <div className="real-bet">
        <h3>{title}</h3>
        <Recorded entry={entry} />
      </div>
    );
  }
  // An automatic Kalshi bet (ADR 0040): its order, never the form; its entry comes from its fills.
  if (alert.exchange) {
    return (
      <div className="real-bet">
        <h3>{title}</h3>
        <Order alert={alert} order={alert.exchange} now={now} />
      </div>
    );
  }
  // Once the game has started the bet is no longer offered; a bet placed before the start can
  // still be recorded, marked late.
  if (now >= new Date(alert.start_utc).getTime()) {
    return (
      <div className="real-bet">
        <h3>{title}</h3>
        <p className="warning">The game has started: don&apos;t place this bet now.</p>
        <details>
          <summary>Placed it before the start? Record it here (marked late)</summary>
          <EntryForm client={client} alert={alert} onRecorded={onRecorded} />
        </details>
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
      <EntryForm client={client} alert={alert} onRecorded={onRecorded} />
    </div>
  );
}

function Order({ alert, order, now }: { alert: RealAlert; order: RealOrder; now: number }) {
  const cents = (limit: string) => Math.round(Number(limit) * 100);
  const size = `${order.contracts} contracts, at most $${Number(order.max_cost_usd).toFixed(2)} (${euros(order.stake)})`;
  // ADR 0045's two parts: a taker part at once, then a maker part resting (#321).
  const maker = order.execution === "adr-0045" ? order.maker : undefined;
  const what =
    order.execution === "adr-0045"
      ? `Kalshi: buy Yes ${team(alert)} at most ${cents(order.limit)}¢ at once, ${size}` +
        (maker
          ? `; then a bid at most ${cents(maker.limit)}¢ for the rest, both parts at most ` +
            `$${Number(maker.max_cost_usd).toFixed(2)} (${euros(maker.stake)})`
          : "")
      : `Kalshi: buy Yes ${team(alert)} at most ${cents(order.limit)}¢, ${size}`;
  if (now < new Date(order.expires_utc).getTime()) {
    const rests = order.execution !== "adr-0045" || maker;
    return (
      <p>
        Placed automatically. {what}
        {rests ? `, resting until ${helsinkiTime(order.expires_utc)}` : ""}. Nothing to enter:
        its fills are recorded once it expires.
      </p>
    );
  }
  return (
    <p className="muted">
      {what}. The order has expired: its fills are recorded at the next run.
    </p>
  );
}

function Recorded({ entry }: { entry: RealEntry }) {
  const late = entry.late ? " Recorded after the start (late)." : "";
  const how = entry.source === "kalshi" ? " automatically, from Kalshi's fills" : "";
  if (entry.outcome === "not placed") {
    return (
      <p className="recorded">
        Recorded{how}: not placed ({entry.reason}).{late}
      </p>
    );
  }
  return (
    <p className="recorded">
      Recorded{how}: placed at {entry.matched_price}, {euros(entry.stake ?? 0)}, bet id{" "}
      {entry.bet_id}, {entry.placed_utc ? helsinkiTime(entry.placed_utc) : ""}.{late}
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
  // Empty means "now", read when the entry is sent: a default fixed when the form mounted would
  // record the page's load time for a bet placed later (Codex on #277).
  const [placedAt, setPlacedAt] = useState("");
  const [reason, setReason] = useState<Reason>(REASONS[0]);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const offered = Number(price);
  const allowed = price && offered > 1 ? stakeAt(alert.stakes, offered) : null;
  const placed = () => (placedAt ? new Date(placedAt) : new Date());
  const bet = () => ({ price: offered, stake: Number(stake), betId, placedAt: placed() });
  const { warnings } = checkPlaced(alert, bet(), new Date());

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
    const sent = bet();
    const checked = checkPlaced(alert, sent, new Date());
    setErrors(checked.errors);
    if (checked.errors.length) return;
    setBusy(true);
    const refused = await recordPlaced(client, alert, sent);
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
          Placed at (your time; empty for now)
          <input
            type="datetime-local"
            step="1"
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
