// Real stakes on the dashboard (#268, ADR 0039): the latest alerted day's bets and the owner's
// form. The rules the form shows are the database's own (supabase/migrations/
// 20261016120000_real_bets.sql): it refuses a second entry, and a placed time at or after the
// game's start, before the alert or in the future, whatever the browser sends. The stakes come
// from the alert's own table, so the dashboard holds no copy of the staking rule.
import type { SupabaseClient } from "@supabase/supabase-js";

import { every } from "./load.ts";
import type { RealAlert, RealCorrection, RealEntry, RealSettlement } from "./types.ts";

export const ALERT_COLUMNS =
  "game_date, game_id, start_utc, home, away, mode, venue, status, side, paper_price, " +
  "min_price, max_stake, bankroll, stakes, alert_utc";
export const ENTRY_COLUMNS =
  "game_date, game_id, outcome, matched_price, stake, bet_id, placed_utc, reason, entered_utc, late";
/** Why an alerted bet wasn't placed, as the migration allows them. */
export const REASONS = ["the price fell under the minimum", "not matched", "missed it"] as const;
export type Reason = (typeof REASONS)[number];
// A day has at most 16 games, so the latest 40 alert rows hold the latest day's.
const LATEST = 40;
// PostgREST's "no such table" and Postgres's "undefined table" or "permission denied": the real
// bets migration isn't applied yet.
const NOT_SET_UP = new Set(["PGRST205", "42P01", "42501"]);

export type RealState =
  | { ready: false }
  | { ready: true; day: string | null; alerts: RealAlert[]; entries: Record<number, RealEntry> };

/** The latest alerted day's rows and the owner's entries for them, read under row-level
 * security; not ready before the migration is applied. Throws Supabase's other errors. */
export async function loadReal(client: SupabaseClient): Promise<RealState> {
  const alerts = await client
    .from("real_alerts")
    .select(ALERT_COLUMNS)
    .order("game_date", { ascending: false })
    .order("start_utc")
    .order("game_id")
    .limit(LATEST)
    .overrideTypes<RealAlert[], { merge: false }>();
  if (alerts.error) {
    if (NOT_SET_UP.has(alerts.error.code)) return { ready: false };
    throw alerts.error;
  }
  const rows = alerts.data ?? [];
  const day = rows[0]?.game_date ?? null;
  if (day === null) return { ready: true, day, alerts: [], entries: {} };
  const entries = await client
    .from("real_entries")
    .select(ENTRY_COLUMNS)
    .eq("game_date", day)
    .overrideTypes<RealEntry[], { merge: false }>();
  if (entries.error) throw entries.error;
  return {
    ready: true,
    day,
    alerts: rows.filter((r) => r.game_date === day),
    entries: Object.fromEntries((entries.data ?? []).map((e) => [e.game_id, e])),
  };
}

/** The stake the alert allows at a price: its row at the highest listed price at or below it,
 * or 0 below the first (no bet). Above the paper price it stays the last row's. */
export function stakeAt(stakes: RealAlert["stakes"], price: number): number {
  let allowed = 0;
  for (const row of stakes) if (row.price <= price + 1e-9) allowed = row.stake;
  return allowed;
}

/** A placed bet as the form holds it, before it is sent. */
export type Placed = { price: number; stake: number; betId: string; placedAt: Date };

/** Why the database would refuse a placed bet (errors), and what puts it outside the rule
 * without refusing it (warnings): a bet placed is recorded as it was, and flagged. */
export function checkPlaced(
  alert: RealAlert,
  bet: Placed,
  now: Date,
): { errors: string[]; warnings: string[] } {
  const errors: string[] = [];
  if (!(bet.price > 1)) errors.push("The matched price must be above 1.");
  if (!(bet.stake > 0)) errors.push("The stake must be above 0.");
  if (!bet.betId.trim()) errors.push("Enter the venue's bet id.");
  const at = bet.placedAt.getTime();
  if (Number.isNaN(at)) errors.push("Enter when you placed it.");
  else if (at >= new Date(alert.start_utc).getTime())
    errors.push("A bet placed at or after the game's start is not the policy's bet.");
  else if (at < new Date(alert.alert_utc).getTime()) errors.push("That is before the alert.");
  else if (at > now.getTime()) errors.push("That time is in the future.");
  const warnings: string[] = [];
  if (bet.price > 1 && bet.price < alert.min_price - 1e-9)
    warnings.push(`The price is below the minimum ${alert.min_price}: the bet is outside the rule.`);
  const allowed = stakeAt(alert.stakes, bet.price);
  if (bet.price >= alert.min_price - 1e-9 && bet.stake > allowed + 1e-9)
    warnings.push(`The rule allows ${allowed.toFixed(2)} € at this price: the stake is above it.`);
  return { errors, warnings };
}

/** Record a placed bet. Returns the database's refusal, or null. */
export async function recordPlaced(
  client: SupabaseClient,
  alert: RealAlert,
  bet: Placed,
): Promise<string | null> {
  const { error } = await client.from("real_entries").insert({
    game_date: alert.game_date,
    game_id: alert.game_id,
    outcome: "placed",
    matched_price: bet.price,
    stake: bet.stake,
    bet_id: bet.betId.trim(),
    placed_utc: bet.placedAt.toISOString(),
  });
  return error ? refusal(error) : null;
}

/** Record that an alerted bet wasn't placed, with its reason. Returns the refusal, or null. */
export async function recordNotPlaced(
  client: SupabaseClient,
  alert: RealAlert,
  reason: Reason,
): Promise<string | null> {
  const { error } = await client
    .from("real_entries")
    .insert({ game_date: alert.game_date, game_id: alert.game_id, outcome: "not placed", reason });
  return error ? refusal(error) : null;
}

function refusal(error: { code?: string; message: string }): string {
  if (error.code === "23505") return "This bet is already recorded: an entry is never changed.";
  return error.message;
}

export const SETTLEMENT_COLUMNS =
  "game_date, game_id, revision, mode, side, matched_price, stake, status, won, profit, " +
  "result_utc, bankroll_after";

/** The whole real record (#270): every alert, entry and settlement revision, or not ready before
 * the real bets migration. settlements is null before the real settlements migration. */
export type RealRecordState =
  | { ready: false }
  | {
      ready: true;
      alerts: RealAlert[];
      entries: RealEntry[];
      corrections: RealCorrection[];
      settlements: RealSettlement[] | null;
    };

/** Every row of the three real tables, a page at a time, under row-level security. */
export async function loadRecord(client: SupabaseClient): Promise<RealRecordState> {
  let alerts: RealAlert[];
  let entries: RealEntry[];
  try {
    [alerts, entries] = await Promise.all([
      every((from, to) =>
        client
          .from("real_alerts")
          .select(ALERT_COLUMNS)
          .order("game_date", { ascending: false })
          .order("game_id")
          .range(from, to)
          .overrideTypes<RealAlert[], { merge: false }>(),
      ),
      every((from, to) =>
        client
          .from("real_entries")
          .select(ENTRY_COLUMNS)
          .order("game_date", { ascending: false })
          .order("game_id")
          .range(from, to)
          .overrideTypes<RealEntry[], { merge: false }>(),
      ),
    ]);
  } catch (error) {
    if (NOT_SET_UP.has((error as { code?: string }).code ?? "")) return { ready: false };
    throw error;
  }
  // Before the real record migration, there are no corrections or settlements to read.
  let corrections: RealCorrection[] = [];
  try {
    corrections = await every((from, to) =>
      client
        .from("real_corrections")
        .select("game_date, game_id, n, changes, reason, corrected_utc")
        .order("corrected_utc")
        .order("game_date")
        .order("game_id")
        .order("n")
        .range(from, to)
        .overrideTypes<RealCorrection[], { merge: false }>(),
    );
  } catch (error) {
    if (!NOT_SET_UP.has((error as { code?: string }).code ?? "")) throw error;
  }
  let settlements: RealSettlement[] | null;
  try {
    settlements = await every((from, to) =>
      client
        .from("real_settlements")
        .select(SETTLEMENT_COLUMNS)
        .order("game_date")
        .order("game_id")
        .order("revision")
        .range(from, to)
        .overrideTypes<RealSettlement[], { merge: false }>(),
    );
  } catch (error) {
    if (!NOT_SET_UP.has((error as { code?: string }).code ?? "")) throw error;
    settlements = null;
  }
  return { ready: true, alerts, entries, corrections, settlements };
}
