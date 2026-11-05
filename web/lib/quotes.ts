// The Kalshi quoting simulator on the dashboard (#398): its figures per game date and variant
// (quote_days) and its nightly figures over every kept day (quote_reports), copied by
// `nhl real quotes --supabase` into owner-only tables. Paper only: a simulator on Kalshi's real
// books, whose fills are an upper bound. Every interval is the report's own (hard rule 7).
import type { SupabaseClient } from "@supabase/supabase-js";

import type { Estimate } from "./format.ts";
import { every } from "./load.ts";

export const RECORD = "kalshi_quotes/1";
export const HOLDS_RECORD = "kalshi_quotes/holds/1";
export const FAIRS = ["model", "anchored", "pinnacle", "forecast", "forecast2"] as const;
export type Fair = (typeof FAIRS)[number];
export const HALF_SPREADS = [0, 1, 2] as const;
export const HOLDS = ["wait", "cool", "return", "trust"] as const;
/** The variant shown first: the model's fair value a cent below, the demo's (ADR 0055). */
export const DEFAULT_FAIR: Fair = "model";
export const DEFAULT_CENTS = 1;
/** The hold rules' primary half-spread (#385). */
export const PRIMARY_CENTS = 1;
/** The live report's minimum weeks for an interval (hard rule 7). */
export const MIN_WEEKS = 4;

export const FAIR_NAMES: Record<Fair, string> = {
  model: "Our model",
  anchored: "Model anchored to Kalshi",
  pinnacle: "Pinnacle",
  forecast: "Close forecast v1",
  forecast2: "Close forecast v2",
};

export const HOLD_NAMES: Record<(typeof HOLDS)[number], string> = {
  wait: "Wait for our fair value",
  cool: "15-minute cool-off",
  return: "Until the move reverses",
  trust: "Never pull (trust)",
};

export const DAY_COLUMNS =
  "game_date, record, variant, fair, half_spread_cents, hold, games, watch_logs, posted, " +
  "pulled_move, pulled_starter, fills, contracts, matched_contracts, largest_inventory, " +
  "cost_usd, fees_usd, edge_fair_usd, last_contracts, edge_last_usd, close_contracts, " +
  "edge_close_usd, settled_contracts, payout_usd, profit_usd, quoted_minutes, late_minutes";

/** One variant's game day (public.quote_days): sums only, no price or fill of its own. */
export type QuoteDay = {
  game_date: string;
  record: string;
  variant: string;
  fair: string;
  half_spread_cents: number;
  hold: string | null;
  games: number;
  watch_logs: boolean;
  posted: number;
  pulled_move: number;
  pulled_starter: number;
  fills: number;
  contracts: number;
  matched_contracts: number;
  largest_inventory: number;
  cost_usd: number;
  fees_usd: number;
  /** The profit our fair value expected at posting, net of the maker fee. */
  edge_fair_usd: number;
  last_contracts: number;
  /** The same by Kalshi's last mid before the start, on last_contracts. */
  edge_last_usd: number;
  close_contracts: number;
  /** The same by Pinnacle's close observed after the fill, on close_contracts. */
  edge_close_usd: number;
  settled_contracts: number;
  payout_usd: number;
  /** Null until every fill's game has its result. */
  profit_usd: number | null;
  quoted_minutes: number | null;
  late_minutes: number | null;
};

/** A variant's figures over every kept day, as kalshi_quotes.summary gives them. */
export type VariantFigures = {
  posted: number;
  pulled_starter: number;
  pulled_move: number;
  fills: number;
  contracts: number;
  matched_contracts: number;
  largest_inventory: number;
  profit_usd: number;
  quoted_minutes?: number;
  late_minutes?: number;
} & Partial<Record<Measure, Estimate | null>>;

export const MEASURES = [
  "spread",
  "markout_5m",
  "markout_30m",
  "markout_last",
  "markout_close",
  "profit",
] as const;
export type Measure = (typeof MEASURES)[number];

export const MEASURE_NAMES: Record<Measure, string> = {
  spread: "Spread at posting",
  markout_5m: "Kalshi's mid 5 minutes later",
  markout_30m: "Kalshi's mid 30 minutes later",
  markout_last: "Kalshi's last mid before the start",
  markout_close: "Pinnacle's close",
  profit: "Profit at settlement, after the fee",
};

export type ReportFigures = {
  as_of_utc?: string;
  code_version?: string;
  days: number;
  days_with_watch_logs: number;
  against_wait?: Record<string, Estimate | Record<string, number>>;
} & Record<string, unknown>;

export type QuoteReport = { as_of: string; record: string; figures: ReportFigures };

export type Quotes = {
  /** False until the quotes migration is applied: the tables don't exist yet. */
  applied: boolean;
  days: QuoteDay[];
  report: QuoteReport | null;
  holds: QuoteReport | null;
};

// PostgREST's "no such table" (and Postgres's): the quotes migration isn't applied yet.
const MISSING = new Set(["PGRST205", "42P01"]);

/** The simulator's and the hold rules' days, and each record's latest night, read under
 * row-level security. A project without the quotes migration gives applied: false. */
export async function loadQuotes(client: SupabaseClient): Promise<Quotes> {
  const latest = (record: string) =>
    client
      .from("quote_reports")
      .select("as_of, record, figures")
      .eq("record", record)
      .order("as_of", { ascending: false })
      .limit(1)
      .overrideTypes<QuoteReport[], { merge: false }>();
  try {
    const [days, report, holds] = await Promise.all([
      every((from, to) =>
        client
          .from("quote_days")
          .select(DAY_COLUMNS)
          .order("game_date")
          .order("record")
          .order("variant")
          .range(from, to)
          .overrideTypes<QuoteDay[], { merge: false }>(),
      ),
      latest(RECORD),
      latest(HOLDS_RECORD),
    ]);
    for (const result of [report, holds]) if (result.error) throw result.error;
    return {
      applied: true,
      days,
      report: report.data?.[0] ?? null,
      holds: holds.data?.[0] ?? null,
    };
  } catch (error) {
    if (MISSING.has((error as { code?: string }).code ?? "")) {
      return { applied: false, days: [], report: null, holds: null };
    }
    throw error;
  }
}

export function variantName(fair: string, cents: number, hold?: string): string {
  return `${fair}-${cents}c${hold ? `-${hold}` : ""}`;
}

/** A variant's totals over its days: the counts, the two-sided share, the settled profit and
 * the profit each reference expected, with the share of contracts it is known for. */
export type Totals = {
  days: number;
  games: number;
  fills: number;
  contracts: number;
  /** Pairs: contracts bought on both teams of a game, the smaller side's. */
  matched: number;
  /** The share of contracts in a pair, both legs counted. */
  twoSided: number | null;
  cost: number;
  fees: number;
  profit: number;
  unsettledDays: number;
  edgeFair: number;
  edgeLast: number;
  lastShare: number | null;
  edgeClose: number;
  closeShare: number | null;
  posted: number;
  pulls: number;
};

export function totals(days: QuoteDay[]): Totals {
  const sum = (f: (d: QuoteDay) => number) => days.reduce((s, d) => s + f(d), 0);
  const contracts = sum((d) => d.contracts);
  const share = (n: number) => (contracts > 0 ? n / contracts : null);
  return {
    days: days.length,
    games: sum((d) => d.games),
    fills: sum((d) => d.fills),
    contracts,
    matched: sum((d) => d.matched_contracts),
    // matched_contracts counts pairs (the smaller team's contracts), so both legs are twice it.
    twoSided: share(2 * sum((d) => d.matched_contracts)),
    cost: sum((d) => d.cost_usd),
    fees: sum((d) => d.fees_usd),
    profit: sum((d) => d.profit_usd ?? 0),
    unsettledDays: days.filter((d) => d.profit_usd === null).length,
    edgeFair: sum((d) => d.edge_fair_usd),
    edgeLast: sum((d) => d.edge_last_usd),
    lastShare: share(sum((d) => d.last_contracts)),
    edgeClose: sum((d) => d.edge_close_usd),
    closeShare: share(sum((d) => d.close_contracts)),
    posted: sum((d) => d.posted),
    pulls: sum((d) => d.pulled_move + d.pulled_starter),
  };
}

/** One variant's days of a record, oldest first. */
export function daysOf(all: QuoteDay[], variant: string, record = RECORD): QuoteDay[] {
  return all
    .filter((d) => d.record === record && d.variant === variant)
    .sort((a, b) => a.game_date.localeCompare(b.game_date));
}

/** The running totals over a variant's game days: the settled profit (null from the first
 * unsettled day on), and the profit our fair value and Pinnacle's close expected. */
export function running(days: QuoteDay[]): {
  dates: string[];
  profit: (number | null)[];
  fair: number[];
  close: number[];
} {
  let profit: number | null = 0;
  let fair = 0;
  let close = 0;
  const out = { dates: [] as string[], profit: [] as (number | null)[], fair: [] as number[], close: [] as number[] };
  for (const d of days) {
    profit = profit === null || d.profit_usd === null ? null : profit + d.profit_usd;
    fair += d.edge_fair_usd;
    close += d.edge_close_usd;
    out.dates.push(d.game_date);
    out.profit.push(profit);
    out.fair.push(fair);
    out.close.push(close);
  }
  return out;
}

/** The game date from which a record's figures can have their interval: the Monday of its
 * first day's week, three weeks on, as the report's weekly blocks count (hard rule 7). A variant
 * whose fills skip a week gets its interval later. */
export function intervalsFrom(first: string): string {
  const day = new Date(`${first}T12:00:00Z`);
  const monday = new Date(day);
  monday.setUTCDate(day.getUTCDate() - ((day.getUTCDay() + 6) % 7) + 7 * (MIN_WEEKS - 1));
  return monday.toISOString().slice(0, 10);
}

/** A variant's figures in a night's report, if it has them. */
export function figuresOf(report: QuoteReport | null, variant: string): VariantFigures | null {
  const found = report?.figures[variant];
  return found && typeof found === "object" ? (found as VariantFigures) : null;
}

/** "$12.34" or "−$1.20", to the cent. */
export function dollars(x: number | null | undefined, signed = false): string {
  if (x === null || x === undefined) return "–";
  const sign = x < 0 ? "−" : signed && x > 0 ? "+" : "";
  return `${sign}$${Math.abs(x).toFixed(2)}`;
}

/** A per-contract figure in cents: "+1.2¢". */
export function cents(x: number | null | undefined): string {
  if (x === null || x === undefined) return "–";
  const v = 100 * x;
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(1)}¢`;
}
