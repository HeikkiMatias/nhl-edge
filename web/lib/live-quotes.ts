// Market making › Today (#398 step 3): the quoting simulator's day as it stands, from the
// owner-only quote_live table that `nhl real quotes-live` upserts every few minutes beside the
// watch. Paper only: fills are an upper bound. Prices are Kalshi's, shown to the owner only.
import type { SupabaseClient } from "@supabase/supabase-js";

import { every } from "./load.ts";

export const LIVE_COLUMNS =
  "game_date, variant, half_spread_cents, game_id, team, home, opponent, start_utc, state, " +
  "fair, yes_bid, yes_ask, top_utc, bid_price, bid_left, contracts, cost_usd, fills, updated_utc";
/** How often the tab reads the table again: the job writes every 5 minutes. */
export const REFRESH_MS = 60_000;
/** Older than this while a game is still to start, the view says it may have stopped. */
export const STALE_MS = 15 * 60_000;

export type State =
  | "waiting"
  | "quoting"
  | "pulled_move"
  | "pulled_starter"
  | "not_quoting"
  | "stopped"
  | "started";

/** One team's market in one variant (public.quote_live). */
export type LiveRow = {
  game_date: string;
  variant: string;
  half_spread_cents: number;
  game_id: number;
  team: string;
  home: boolean;
  opponent: string;
  start_utc: string;
  state: State;
  fair: number | null;
  yes_bid: number | null;
  yes_ask: number | null;
  top_utc: string | null;
  bid_price: number | null;
  bid_left: number | null;
  contracts: number;
  cost_usd: number;
  fills: number;
  updated_utc: string;
};

export type Live = { applied: boolean; day: string | null; rows: LiveRow[] };

const MISSING = new Set(["PGRST205", "42P01"]);

/** The latest game date's rows, read under row-level security, or applied: false before the
 * live migration. */
export async function loadLive(client: SupabaseClient): Promise<Live> {
  try {
    const latest = await client
      .from("quote_live")
      .select("game_date")
      .order("game_date", { ascending: false })
      .limit(1)
      .overrideTypes<{ game_date: string }[], { merge: false }>();
    if (latest.error) throw latest.error;
    const day = latest.data?.[0]?.game_date ?? null;
    if (day === null) return { applied: true, day: null, rows: [] };
    const rows = await every((from, to) =>
      client
        .from("quote_live")
        .select(LIVE_COLUMNS)
        .eq("game_date", day)
        .order("start_utc")
        .order("game_id")
        .order("team")
        .range(from, to)
        .overrideTypes<LiveRow[], { merge: false }>(),
    );
    return { applied: true, day, rows };
  } catch (error) {
    if (MISSING.has((error as { code?: string }).code ?? "")) {
      return { applied: false, day: null, rows: [] };
    }
    throw error;
  }
}

/** A game of the variant: its away and home markets, and what its holding pays either way. */
export type LiveGame = {
  game_id: number;
  start_utc: string;
  away: LiveRow;
  home: LiveRow;
  cost: number;
  /** The holding's profit if the home team wins, and if the away team does, on the full game. */
  ifHome: number;
  ifAway: number;
  /** Contracts bought on both teams: a dollar each whatever happens. */
  pairs: number;
};

export function gamesOf(rows: LiveRow[], halfSpread: number): LiveGame[] {
  const mine = rows.filter((r) => r.half_spread_cents === halfSpread);
  const byGame = new Map<number, { home?: LiveRow; away?: LiveRow }>();
  for (const r of mine) {
    const g = byGame.get(r.game_id) ?? {};
    if (r.home) g.home = r;
    else g.away = r;
    byGame.set(r.game_id, g);
  }
  const games: LiveGame[] = [];
  for (const [game_id, { home, away }] of byGame) {
    if (!home || !away) continue;
    const cost = home.cost_usd + away.cost_usd;
    games.push({
      game_id,
      start_utc: home.start_utc,
      home,
      away,
      cost,
      ifHome: home.contracts - cost,
      ifAway: away.contracts - cost,
      pairs: Math.min(home.contracts, away.contracts),
    });
  }
  return games.sort((a, b) => a.start_utc.localeCompare(b.start_utc) || a.game_id - b.game_id);
}

/** The day's totals: what is held, at what cost, and the best and worst the results can give. */
export function dayTotals(games: LiveGame[]) {
  const sum = (f: (g: LiveGame) => number) => games.reduce((s, g) => s + f(g), 0);
  return {
    games: games.length,
    quoting: games.filter((g) => g.home.state === "quoting" || g.away.state === "quoting").length,
    fills: sum((g) => g.home.fills + g.away.fills),
    contracts: sum((g) => g.home.contracts + g.away.contracts),
    cost: sum((g) => g.cost),
    best: sum((g) => Math.max(g.ifHome, g.ifAway)),
    worst: sum((g) => Math.min(g.ifHome, g.ifAway)),
  };
}

/** The newest cycle's time among the rows. */
export function updatedAt(rows: LiveRow[]): string | null {
  return rows.reduce<string | null>((t, r) => (t === null || r.updated_utc > t ? r.updated_utc : t), null);
}

/** Whether the rows look stopped: older than STALE_MS while a game is still to start. */
export function stale(rows: LiveRow[], now: Date): boolean {
  const at = updatedAt(rows);
  if (at === null) return false;
  const open = rows.some((r) => new Date(r.start_utc) > now);
  return open && now.getTime() - new Date(at).getTime() > STALE_MS;
}

export const STATE_NAMES: Record<State, string> = {
  waiting: "Waiting for our fair value",
  quoting: "Quoting",
  pulled_move: "Pulled: the price moved",
  pulled_starter: "Pulled: a new starter",
  not_quoting: "Not quoting",
  stopped: "Stopped before the start",
  started: "Started",
};

/** A price in cents, "43¢", or "–". */
export function priceCents(p: number | null | undefined): string {
  return p === null || p === undefined ? "–" : `${Math.round(100 * p)}¢`;
}
