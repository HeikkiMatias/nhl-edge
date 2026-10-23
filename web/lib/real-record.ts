// The real record beside the paper one (#270, ADR 0039): each paper bet's real twin, the real
// bankroll and the price the real bets got against the paper price. Every figure is a sum or a
// difference of stored columns (the alerts, the owner's entries and the settlements), so the
// dashboard holds no rule of its own (ADR 0034). Practice and real are never mixed.

import type {
  Bet,
  RealAlert,
  RealCorrection,
  RealDayMode,
  RealEntry,
  RealSettlement,
} from "./types.ts";

/** The real bankroll's start, in euros, and the fall from it that stops real bets (ADR 0039). */
export const REAL_START = 100;
export const STOP_DRAWDOWN = 0.2;

/** Which record a view shows: practice before the start, real from it. */
export type View = "practice" | "real";
// A window that closed without a start ends the practice history (Codex on #278).
const MODES: Record<View, ReadonlySet<RealAlert["mode"]>> = {
  practice: new Set(["practice", "closed"]),
  real: new Set(["real", "paused", "stopped"]),
};

/** Since when real bets run, and the latest real day with its mode: null before the start. */
export function since(
  alerts: RealAlert[],
  days: RealDayMode[] = [],
): { start: string; latest: string; mode: RealAlert["mode"] } | null {
  const modes = dayModes(alerts, days);
  const real = datesOf(modes, MODES.real);
  if (real.length === 0) return null;
  const latest = real[real.length - 1];
  return { start: real[0], latest, mode: modes.get(latest) ?? "real" };
}

/** The entries with the corrections applied in order, as the R2 record's real_bets applies
 * them: a correction changes its entry's fields, or makes one for a bet placed but never
 * recorded, entered at the correction's time. corrected counts them per bet. */
export function corrected(
  entries: RealEntry[],
  corrections: RealCorrection[],
  alerts: RealAlert[],
): { entries: RealEntry[]; counts: Map<string, number> } {
  const byKey = new Map(entries.map((e) => [key(e.game_date, e.game_id), { ...e }]));
  const starts = new Map(alerts.map((a) => [key(a.game_date, a.game_id), a.start_utc]));
  const counts = new Map<string, number>();
  const ordered = [...corrections].sort((a, b) =>
    a.corrected_utc !== b.corrected_utc
      ? a.corrected_utc < b.corrected_utc ? -1 : 1
      : a.game_date !== b.game_date
        ? a.game_date < b.game_date ? -1 : 1
        : a.game_id !== b.game_id
          ? a.game_id - b.game_id
          : a.n - b.n,
  );
  for (const c of ordered) {
    const k = key(c.game_date, c.game_id);
    const start = starts.get(k);
    const base: RealEntry = byKey.get(k) ?? {
      game_date: c.game_date,
      game_id: c.game_id,
      outcome: "placed",
      matched_price: null,
      stake: null,
      bet_id: null,
      placed_utc: null,
      reason: null,
      entered_utc: c.corrected_utc,
      late: start !== undefined && new Date(c.corrected_utc) >= new Date(start),
    };
    const next = { ...base, ...c.changes };
    // A bet found not placed keeps no placement, as the record's checks require.
    if (next.outcome === "not placed") {
      Object.assign(next, { matched_price: null, stake: null, bet_id: null, placed_utc: null });
    }
    byKey.set(k, next);
    counts.set(k, (counts.get(k) ?? 0) + 1);
  }
  return { entries: [...byKey.values()], counts };
}

/** Each alerted day's mode: from real_days, which holds days without bets too (Codex on #278),
 * and from the alert rows of any day it lacks. */
export function dayModes(alerts: RealAlert[], days: RealDayMode[] = []): Map<string, RealAlert["mode"]> {
  const out = new Map<string, RealAlert["mode"]>();
  for (const a of alerts) out.set(a.game_date, a.mode);
  for (const d of days) out.set(d.game_date, d.mode);
  return out;
}

const datesOf = (modes: Map<string, RealAlert["mode"]>, wanted: ReadonlySet<RealAlert["mode"]>) =>
  [...modes.entries()].filter(([, m]) => wanted.has(m)).map(([d]) => d).sort();

/** The view the record shows: real once any day was alerted in a real mode, else practice. */
export function view(alerts: RealAlert[], days: RealDayMode[] = []): View {
  return datesOf(dayModes(alerts, days), MODES.real).length ? "real" : "practice";
}

const key = (game_date: string, game_id: number) => `${game_date}/${game_id}`;

/** The latest revision of each bet's settlement, by bet. */
export function latest(settlements: RealSettlement[]): Map<string, RealSettlement> {
  const out = new Map<string, RealSettlement>();
  for (const s of settlements) {
    const k = key(s.game_date, s.game_id);
    const seen = out.get(k);
    if (!seen || s.revision > seen.revision) out.set(k, s);
  }
  return out;
}

export type TwinState = "placed" | "not placed" | "skipped" | "not recorded" | "no alert";

/** A paper bet with its real twin: its alert, the owner's entry and the latest settlement. */
export type Twin = {
  bet: Bet;
  alert: RealAlert | null;
  entry: RealEntry | null;
  settlement: RealSettlement | null;
  state: TwinState;
};

/** Each paper bet from the view's first alerted day on, newest first as the bets come, with its
 * twin: placed, not placed, skipped with the alert's reason, not recorded, or no alert at all
 * (a day whose alert didn't run). */
export function twins(
  bets: Bet[],
  alerts: RealAlert[],
  entries: RealEntry[],
  settlements: RealSettlement[],
  shown: View,
  days: RealDayMode[] = [],
): Twin[] {
  const modes = dayModes(alerts, days);
  const dates = datesOf(modes, MODES[shown]);
  if (dates.length === 0) return [];
  const first = dates[0];
  // A practice view ends where the real record begins, or on the day the window closed, after
  // which no alert runs (Codex on #278).
  const real = datesOf(modes, MODES.real);
  const closed = datesOf(modes, new Set(["closed"]));
  const inView = (d: string) =>
    d >= first &&
    (shown === "real" ||
      ((real.length === 0 || d < real[0]) && (closed.length === 0 || d <= closed[0])));
  const ours = alerts.filter((a) => MODES[shown].has(a.mode));
  const byAlert = new Map(ours.map((a) => [key(a.game_date, a.game_id), a]));
  const byEntry = new Map(entries.map((e) => [key(e.game_date, e.game_id), e]));
  const settled = latest(settlements);
  return bets
    .filter((b) => inView(b.game_date))
    .map((bet) => {
      const k = key(bet.game_date, bet.game_id);
      const alert = byAlert.get(k) ?? null;
      const entry = byEntry.get(k) ?? null;
      const state: TwinState =
        alert === null
          ? "no alert"
          : alert.status !== "alerted"
            ? "skipped"
            : entry === null
              ? "not recorded"
              : entry.outcome;
      return { bet, alert, entry, settlement: settled.get(k) ?? null, state };
    });
}

/** The bankroll after a settled day, from 100 €. */
export type RealDay = { date: string; bets: number; profit: number; balance: number };

/** The view's bankroll after each game day with a settled bet, oldest first: 100 € plus the
 * latest revision's profit of each settled or paid-out bet (void and cancelled ones count 0). */
export function realBankroll(settlements: RealSettlement[], shown: View): RealDay[] {
  const mode = shown === "real" ? "real" : "practice";
  const byDay = new Map<string, { bets: number; profit: number }>();
  for (const s of latest(settlements).values()) {
    if (s.mode !== mode) continue;
    const day = byDay.get(s.game_date) ?? { bets: 0, profit: 0 };
    day.bets += s.status === "settled" || s.status === "payout" ? 1 : 0;
    day.profit += s.profit;
    byDay.set(s.game_date, day);
  }
  let balance = REAL_START;
  return [...byDay.entries()]
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([date, day]) => {
      balance += day.profit;
      return { date, ...day, balance };
    });
}

/** The view's record from the latest revisions: won, lost, void, paid out at Kalshi's own price
 * (#290), the stakes settled or paid out and the profit. */
export function realRecord(settlements: RealSettlement[], shown: View) {
  const mode = shown === "real" ? "real" : "practice";
  const rows = [...latest(settlements).values()].filter((s) => s.mode === mode);
  const settled = rows.filter((s) => s.status === "settled");
  const paidOut = rows.filter((s) => s.status === "payout");
  return {
    won: settled.filter((s) => s.won === true).length,
    lost: settled.filter((s) => s.won === false).length,
    void: rows.filter((s) => s.status === "void").length,
    payout: paidOut.length,
    staked: [...settled, ...paidOut].reduce((sum, s) => sum + s.stake, 0),
    profit: rows.reduce((sum, s) => sum + s.profit, 0),
  };
}

/** The price the placed bets got against the paper price: per bet, the matched price less the
 * paper price; in total, the profit the settled won bets made at their matched prices less what
 * the same stakes would have made at the paper prices (a lost bet loses its stake at any price). */
export function priceGap(rows: Twin[]): {
  bets: { twin: Twin; difference: number }[];
  mean: number | null;
  profit: number;
} {
  const placed = rows.filter((t) => t.state === "placed" && t.entry?.matched_price != null);
  const bets = placed.map((twin) => ({
    twin,
    difference: (twin.entry?.matched_price ?? 0) - twin.bet.price,
  }));
  const mean = bets.length ? bets.reduce((s, b) => s + b.difference, 0) / bets.length : null;
  const profit = placed.reduce((sum, t) => {
    const s = t.settlement;
    if (!s || s.status !== "settled" || s.won !== true) return sum;
    return sum + s.stake * (s.matched_price - t.bet.price);
  }, 0);
  return { bets, mean, profit };
}
