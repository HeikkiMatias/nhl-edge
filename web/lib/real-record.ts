// The real record beside the paper one (#270, ADR 0039): each paper bet's real twin, the real
// bankroll and the price the real bets got against the paper price. Every figure is a sum or a
// difference of stored columns (the alerts, the owner's entries and the settlements), so the
// dashboard holds no rule of its own (ADR 0034). Practice and real are never mixed.

import type { Bet, RealAlert, RealEntry, RealSettlement } from "./types.ts";

/** The real bankroll's start, in euros, and the fall from it that stops real bets (ADR 0039). */
export const REAL_START = 100;
export const STOP_DRAWDOWN = 0.2;

/** Which record a view shows: practice before the start, real from it. */
export type View = "practice" | "real";
const MODES: Record<View, ReadonlySet<RealAlert["mode"]>> = {
  practice: new Set(["practice"]),
  real: new Set(["real", "paused", "stopped"]),
};

/** The view the record shows: real once any day was alerted in a real mode, else practice. */
export function view(alerts: RealAlert[]): View {
  return alerts.some((a) => MODES.real.has(a.mode)) ? "real" : "practice";
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
): Twin[] {
  const ours = alerts.filter((a) => MODES[shown].has(a.mode));
  if (ours.length === 0) return [];
  const first = ours.reduce((d, a) => (a.game_date < d ? a.game_date : d), ours[0].game_date);
  // A practice view ends where the real record begins.
  const real = alerts.filter((a) => MODES.real.has(a.mode)).map((a) => a.game_date);
  const end = shown === "practice" && real.length ? real.reduce((d, x) => (x < d ? x : d)) : null;
  const byAlert = new Map(ours.map((a) => [key(a.game_date, a.game_id), a]));
  const byEntry = new Map(entries.map((e) => [key(e.game_date, e.game_id), e]));
  const settled = latest(settlements);
  return bets
    .filter((b) => b.game_date >= first && (end === null || b.game_date < end))
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
 * latest revision's profit of each settled bet (void and cancelled ones count 0). */
export function realBankroll(settlements: RealSettlement[], shown: View): RealDay[] {
  const mode = shown === "real" ? "real" : "practice";
  const byDay = new Map<string, { bets: number; profit: number }>();
  for (const s of latest(settlements).values()) {
    if (s.mode !== mode) continue;
    const day = byDay.get(s.game_date) ?? { bets: 0, profit: 0 };
    day.bets += s.status === "settled" ? 1 : 0;
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

/** The view's record from the latest revisions: won, lost, void, the stakes settled and the
 * profit. */
export function realRecord(settlements: RealSettlement[], shown: View) {
  const mode = shown === "real" ? "real" : "practice";
  const rows = [...latest(settlements).values()].filter((s) => s.mode === mode);
  const settled = rows.filter((s) => s.status === "settled");
  return {
    won: settled.filter((s) => s.won === true).length,
    lost: settled.filter((s) => s.won === false).length,
    void: rows.filter((s) => s.status === "void").length,
    staked: settled.reduce((sum, s) => sum + s.stake, 0),
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
