// The Models tab (#398 step 5): the model against the market and each challenger over time, from
// the nightly live reports already in Supabase (live_reports), read only when the tab first
// opens. A point is drawn only once its figure has its interval, from 4 weeks (hard rule 7), and
// nothing here gives a verdict before the formal review (ADR 0032).
import type { SupabaseClient } from "@supabase/supabase-js";

import type { Estimate } from "./format.ts";
import { every } from "./load.ts";
import type { Figure } from "./types.ts";

/** The live report's interim floor: an estimate needs this many weeks of games. */
export const MIN_WEEKS = 4;

/** A challenger's entry in a nightly report (live/report.py's challengers section). */
export type ChallengerEntry = {
  name: string;
  version: string;
  adr: string;
  registered_on: string;
  /** Its primary measure (ADR 0038 rule 4): what its log loss is paired with, or "move"; null for
   * the control. Absent from reports before #398 step 5. */
  primary?: string | null;
  baseline?: string;
  forecasts?: "result" | "close";
  days: number;
  games: number;
  predicted: number;
  not_predicted: Record<string, number>;
  missing_days: string[];
  pending_days: string[];
  /** The days its baseline challenger lacks, which withhold the pairing (ADR 0042). */
  baseline_missing_days?: string[];
  latest_fit?: { games?: number; minimum?: number; week?: string } & Record<string, unknown>;
  comparisons?: Record<string, { difference: Figure; left_out: number }>;
  move?: { difference: Figure };
  shadow?: { bets: number; clv_per_bet: Figure };
};

/** The morning view's or a close forecaster's section: its registration, days and primary. */
export type ForecastSection = {
  version: string;
  registered_on: string;
  days: number;
  games: number;
  predicted: number;
  not_predicted: Record<string, number>;
  missing_days: string[];
  primary?: Figure;
};

/** One night's figures the tab charts. */
export type ModelNight = {
  as_of: string;
  comparisons: Record<string, { difference: Figure; left_out: number }> | null;
  slope: Figure | null;
  challengers: Record<string, ChallengerEntry> | null;
  morning: Figure | null;
  close1: Figure | null;
  close2: Figure | null;
};

export const HISTORY_COLUMNS =
  "as_of, comparisons:report->comparisons, slope:report->calibration->slope, " +
  "challengers:report->challengers->challengers, morning:report->morning_view->primary, " +
  "close1:report->close_forecast->primary, close2:report->close_forecast_2->primary";

/** Every nightly report of the policy, oldest first, with only the figures the tab charts. */
export async function loadModels(client: SupabaseClient, policy: string): Promise<ModelNight[]> {
  return every((from, to) =>
    client
      .from("live_reports")
      .select(HISTORY_COLUMNS)
      .eq("policy_version", policy)
      .order("as_of")
      .range(from, to)
      .overrideTypes<ModelNight[], { merge: false }>(),
  );
}

/** The model a challenger's primary measure is paired with, or "move", or null for the control.
 * Reports before the field fall back on its ADR's rule (live/challengers.REGISTRY): a close
 * forecaster's move, its baseline, the control's none, and the frozen blend otherwise. */
export function primaryOf(entry: ChallengerEntry): string | null {
  if (entry.primary !== undefined) return entry.primary;
  if (entry.forecasts === "close") return "move";
  if (entry.baseline) return entry.baseline;
  return entry.name === "market_only" ? null : "BLEND";
}

/** A challenger's primary figure in a night's entry, or null while withheld or the control's. */
export function primaryFigure(entry: ChallengerEntry | undefined): Figure | null {
  if (!entry) return null;
  const primary = primaryOf(entry);
  if (primary === null) return null;
  if (primary === "move") return entry.move?.difference ?? null;
  return entry.comparisons?.[primary]?.difference ?? null;
}

/** What a challenger's primary measure is, in words. */
export function primaryName(entry: ChallengerEntry): string {
  const primary = primaryOf(entry);
  if (primary === null) return "The control: no primary measure, so no candidate";
  if (primary === "move") {
    return "Its forecast's squared error on the move to Pinnacle's close less no move's (below 0 favours it)";
  }
  const against = primary === "BLEND" ? "the frozen blend" : primary;
  return `Log loss against ${against}, the challenger's less (below 0 favours it)`;
}

/** Whether a figure has its interval: a point the charts may draw. */
export function hasInterval(f: Figure | Estimate | null | undefined): boolean {
  return !!f && (f.mean ?? f.value) !== undefined && f.low !== undefined && f.high !== undefined;
}

/** The nights whose figure has its interval, as chart points. */
export function series(
  nights: ModelNight[],
  pick: (night: ModelNight) => Figure | null | undefined,
): { as_of: string; value: number; low: number; high: number }[] {
  const out = [];
  for (const night of nights) {
    const f = pick(night);
    if (!f || !hasInterval(f)) continue;
    out.push({ as_of: night.as_of, value: (f.mean ?? f.value) as number, low: f.low as number, high: f.high as number });
  }
  return out;
}

/** About when a figure gets its interval: the weeks it still lacks of MIN_WEEKS, from its
 * report's date. Null once it has one. */
export function intervalAbout(f: Figure | null | undefined, asOf: string): string | null {
  if (hasInterval(f)) return null;
  const weeks = f?.weeks ?? 0;
  const day = new Date(`${asOf}T12:00:00Z`);
  day.setUTCDate(day.getUTCDate() + 7 * Math.max(1, MIN_WEEKS - weeks));
  return day.toISOString().slice(0, 10);
}

/** A fitted challenger's training games toward its minimum, as a share, or null if it doesn't
 * fit. */
export function fitProgress(entry: ChallengerEntry): { games: number; minimum: number; share: number } | null {
  const fit = entry.latest_fit;
  if (!fit || typeof fit.games !== "number" || typeof fit.minimum !== "number" || fit.minimum <= 0) {
    return null;
  }
  return { games: fit.games, minimum: fit.minimum, share: Math.min(1, fit.games / fit.minimum) };
}

/** Where a day stands between a registration and the formal review: the share of the way. */
export function elapsed(registered: string, review: string, today: string): number {
  const start = Date.parse(`${registered}T00:00:00Z`);
  const end = Date.parse(`${review}T00:00:00Z`);
  const now = Date.parse(`${today}T00:00:00Z`);
  if (!(end > start)) return 1;
  return Math.min(1, Math.max(0, (now - start) / (end - start)));
}
