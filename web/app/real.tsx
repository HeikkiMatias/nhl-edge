"use client";

import type { SupabaseClient } from "@supabase/supabase-js";
import { type FormEvent, useCallback, useEffect, useState } from "react";

import { helsinkiTime } from "@/lib/format";
import {
  checkNote,
  checkPlaced,
  loadReal,
  NOTE_MAX,
  REASONS,
  type Reason,
  type RealState,
  recordNotPlaced,
  recordPlaced,
  reviseNotPlaced,
  revisePlaced,
  stakeAt,
  standing,
  withdraw,
} from "@/lib/real";
import type { RealAlert, RealCorrection, RealEntry, RealOrder, RealRevision } from "@/lib/types";

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
          revisions={state.revisions === null ? null : (state.revisions[alert.game_id] ?? [])}
          corrections={state.corrections[alert.game_id] ?? []}
          onRecorded={refresh}
          now={now}
        />
      ))}
      <Explain
        terms={["realStakes", "practice", "minPrice", "realStake", "realRecord", "realRevision"]}
      />
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
  revisions,
  corrections,
  onRecorded,
  now,
}: {
  client: SupabaseClient;
  alert: RealAlert;
  entry: RealEntry | undefined;
  /** The entry's revisions in order, or null before their migration (ADR 0058). */
  revisions: RealRevision[] | null;
  /** The weekly check's corrections of the bet, in order. */
  corrections: RealCorrection[];
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
  if (entry || corrections.length) {
    // The entry as the owner's revisions, then the weekly check's corrections, leave it, as the
    // record applies them (ADR 0058): until the start the form's own entries can be corrected or
    // undone, with a note; from the start on only the weekly check corrects them. Kalshi's
    // automatic entries are the pipeline's, from its fills.
    const revised = entry ? (revisions ?? []) : [];
    const current = standing(entry ?? null, revised, corrections, alert);
    const open =
      entry !== undefined &&
      revisions !== null &&
      entry.source !== "kalshi" &&
      now < new Date(alert.start_utc).getTime();
    return (
      <div className="real-bet">
        <h3>{title}</h3>
        {current ? (
          <Recorded
            entry={current}
            revised={revised.length > 0}
            corrected={corrections.length > 0}
          />
        ) : (
          <p className="recorded">Withdrawn: the bet is not recorded.</p>
        )}
        {open ? (
          <Revise client={client} alert={alert} current={current} onRecorded={onRecorded} />
        ) : !current ? (
          <p className="muted">
            The game has started: from now on only the weekly check, against the venue&apos;s
            statement, records it.
          </p>
        ) : null}
        {revised.length || corrections.length ? (
          <History entry={entry} revisions={revised} corrections={corrections} />
        ) : null}
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

function Recorded({
  entry,
  revised = false,
  corrected = false,
}: {
  entry: RealEntry;
  revised?: boolean;
  corrected?: boolean;
}) {
  const late = entry.late ? " Recorded after the start (late)." : "";
  const how = entry.source === "kalshi" ? " automatically, from Kalshi's fills" : "";
  const again = revised ? " Revised before the start." : "";
  const fixed = corrected ? " Corrected at the weekly check." : "";
  return (
    <p className="recorded">
      Recorded{how}: {described(entry)}.{late}
      {again}
      {fixed}
    </p>
  );
}

/** An entry or a revision in words: placed at its price and stake, or not placed and why. */
function described(e: Pick<RealEntry, "matched_price" | "stake" | "bet_id" | "placed_utc" | "reason"> & {
  outcome: RealRevision["outcome"];
}): string {
  if (e.outcome === "withdrawn") return "withdrawn, the bet unrecorded";
  if (e.outcome === "not placed") return `not placed (${e.reason})`;
  const at = e.placed_utc ? `, ${helsinkiTime(e.placed_utc)}` : "";
  return `placed at ${e.matched_price}, ${euros(e.stake ?? 0)}, bet id ${e.bet_id}${at}`;
}

/** Correct an entry or undo it until the game's start (ADR 0058), each with a note; a
 * withdrawn bet can be recorded anew. Nothing is edited: each is appended beside the entry. */
function Revise({
  client,
  alert,
  current,
  onRecorded,
}: {
  client: SupabaseClient;
  alert: RealAlert;
  current: RealEntry | null;
  onRecorded: () => void;
}) {
  if (!current) {
    return (
      <details>
        <summary>Record it again</summary>
        <EntryForm client={client} alert={alert} onRecorded={onRecorded} revision />
      </details>
    );
  }
  return (
    <>
      <details>
        <summary>Correct it</summary>
        <EntryForm
          client={client}
          alert={alert}
          onRecorded={onRecorded}
          initial={current}
          revision
        />
      </details>
      <details>
        <summary>Undo it</summary>
        <Undo client={client} alert={alert} onRecorded={onRecorded} />
      </details>
    </>
  );
}

function Undo({
  client,
  alert,
  onRecorded,
}: {
  client: SupabaseClient;
  alert: RealAlert;
  onRecorded: () => void;
}) {
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function undo(event: FormEvent) {
    event.preventDefault();
    const wrong = checkNote(note);
    setError(wrong);
    if (wrong) return;
    setBusy(true);
    const refused = await withdraw(client, alert, note);
    setBusy(false);
    if (refused) setError(refused);
    else onRecorded();
  }

  return (
    <>
      <form onSubmit={undo} aria-label={`Undo ${team(alert)}`}>
        <Note value={note} onChange={setNote} />
        <button type="submit" className="secondary" disabled={busy}>
          Undo the entry
        </button>
      </form>
      {error ? (
        <p className="error" role="alert">
          {error}
        </p>
      ) : null}
    </>
  );
}

function Note({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <label className="note">
      Why (kept with the record)
      <input
        required
        maxLength={NOTE_MAX}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** The entry, each revision of it with its note, and each correction at the weekly check with
 * its reason, oldest first (ADR 0058). */
function History({
  entry,
  revisions,
  corrections,
}: {
  entry: RealEntry | undefined;
  revisions: RealRevision[];
  corrections: RealCorrection[];
}) {
  const parts = [
    entry ? "the entry" : "no entry",
    ...(revisions.length ? [plural(revisions.length, "revision")] : []),
    ...(corrections.length ? [plural(corrections.length, "correction")] : []),
  ];
  return (
    <details className="history">
      <summary>History: {parts.join(", ")}</summary>
      <ol>
        {entry ? (
          <li>
            Entry, {helsinkiTime(entry.entered_utc)}: {described(entry)}.
          </li>
        ) : null}
        {revisions.map((r) => (
          <li key={`r${r.n}`}>
            Revision {r.n}, {helsinkiTime(r.revised_utc)}: {described(r)}. Why: {r.note}
          </li>
        ))}
        {corrections.map((c) => (
          <li key={`c${c.n}`}>
            Correction at the weekly check, {c.corrected_utc.slice(0, 10)}: {changed(c.changes)}.
            Why: {c.reason}
          </li>
        ))}
      </ol>
    </details>
  );
}

const FIELD_NAMES: Record<string, string> = {
  outcome: "outcome",
  matched_price: "price",
  stake: "stake",
  bet_id: "bet id",
  placed_utc: "placed at",
  reason: "reason",
};

/** A correction's changes in words: "stake 1.30, bet id X2". */
function changed(changes: RealCorrection["changes"]): string {
  return Object.entries(changes)
    .map(([name, value]) => {
      const shown =
        value === null
          ? "none"
          : name === "stake"
            ? euros(Number(value))
            : name === "placed_utc"
              ? helsinkiTime(String(value))
              : String(value);
      return `${FIELD_NAMES[name] ?? name} ${shown}`;
    })
    .join(", ");
}

/** An instant as the datetime-local input shows it, in the browser's own time. */
function localInput(iso: string): string {
  const at = new Date(iso);
  return new Date(at.getTime() - at.getTimezoneOffset() * 60_000).toISOString().slice(0, 19);
}

function EntryForm({
  client,
  alert,
  onRecorded,
  initial = null,
  revision = false,
}: {
  client: SupabaseClient;
  alert: RealAlert;
  onRecorded: () => void;
  /** The entry as it stands, which a correction starts from. */
  initial?: RealEntry | null;
  /** A revision of the entry (ADR 0058), sent with its note, rather than the entry itself. */
  revision?: boolean;
}) {
  const placedBefore = initial?.outcome === "placed";
  const [price, setPrice] = useState(placedBefore ? String(initial.matched_price ?? "") : "");
  const [stake, setStake] = useState(
    placedBefore && initial.stake !== null ? initial.stake.toFixed(2) : "",
  );
  // A correction keeps the stake recorded unless it is changed, whatever the price.
  const [stakeEdited, setStakeEdited] = useState(placedBefore);
  const [betId, setBetId] = useState(placedBefore ? (initial.bet_id ?? "") : "");
  // Empty means "now", read when the entry is sent: a default fixed when the form mounted would
  // record the page's load time for a bet placed later (Codex on #277).
  const [placedAt, setPlacedAt] = useState(
    placedBefore && initial.placed_utc ? localInput(initial.placed_utc) : "",
  );
  const [reason, setReason] = useState<Reason>(
    initial?.outcome === "not placed" && REASONS.includes(initial.reason as Reason)
      ? (initial.reason as Reason)
      : REASONS[0],
  );
  const [note, setNote] = useState("");
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

  // A revision needs its note, for either outcome.
  const noted = () => (revision ? checkNote(note) : null);

  async function place(event: FormEvent) {
    event.preventDefault();
    const sent = bet();
    const checked = checkPlaced(alert, sent, new Date());
    const unsaid = noted();
    const wrong = unsaid ? [...checked.errors, unsaid] : checked.errors;
    setErrors(wrong);
    if (wrong.length) return;
    setBusy(true);
    const refused = revision
      ? await revisePlaced(client, alert, sent, note)
      : await recordPlaced(client, alert, sent);
    setBusy(false);
    if (refused) setErrors([refused]);
    else onRecorded();
  }

  async function notPlaced(event: FormEvent) {
    event.preventDefault();
    const wrong = noted();
    setErrors(wrong ? [wrong] : []);
    if (wrong) return;
    setBusy(true);
    const refused = revision
      ? await reviseNotPlaced(client, alert, reason, note)
      : await recordNotPlaced(client, alert, reason);
    setBusy(false);
    if (refused) setErrors([refused]);
    else onRecorded();
  }

  return (
    <>
      {revision ? <Note value={note} onChange={setNote} /> : null}
      <form onSubmit={place} aria-label={`${revision ? "Revise" : "Record"} ${team(alert)}`}>
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
          {revision ? "Save as placed" : "Record placed bet"}
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
      <form
        onSubmit={notPlaced}
        aria-label={`${revision ? "Revise as not placed" : "Not placed"} ${team(alert)}`}
      >
        <select value={reason} onChange={(e) => setReason(e.target.value as Reason)}>
          {REASONS.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <button type="submit" className="secondary" disabled={busy}>
          {revision ? "Save as not placed" : "Record not placed"}
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
