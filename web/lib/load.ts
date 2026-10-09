import type { SupabaseClient } from "@supabase/supabase-js";

import type { Bet, Figure, LiveReport, Prediction, ReportRow, Score } from "./types.ts";

export const PREDICTION_COLUMNS =
  "game_date, game_id, start_utc, home, away, status, home_price, away_price, p_b1, p_b3, " +
  "p_blend, bet";
// With each bet's result and profit, shown during the season (ADR 0034).
export const BET_COLUMNS =
  "game_date, game_id, start_utc, home, away, side, price, p_side, ev, bankroll, stake, " +
  "settled_utc, settlement, won, profit, close_status, clv, fair_move";
export const SCORE_COLUMNS = "game_id, home_score, away_score, decided_in";
// Postgres's "permission denied": the games read migration (#210) isn't applied yet.
const NOT_GRANTED = "42501";
// A day's slate is at most 16 games, so the latest 40 decisions hold the latest day's.
const LATEST_DECISIONS = 40;
// PostgREST caps each response at the project's max rows (1,000 by default), so the whole
// ledger and history are read a page at a time.
export const PAGE = 1000;

/** What the dashboard shows: nothing for a signed-in user who isn't an owner. */
export type Data = {
  owner: boolean;
  day: string | null;
  slate: Prediction[];
  bets: Bet[];
  report: LiveReport | null;
  history: { as_of: string; clv: Figure }[];
  /** Each settled bet's final score, by game id; null while the owner can't read games. */
  scores: Record<number, Score> | null;
};

const NOTHING: Data = {
  owner: false,
  day: null,
  slate: [],
  bets: [],
  report: null,
  history: [],
  scores: {},
};

type Page<T> = PromiseLike<{ data: T[] | null; error: unknown }>;

/** Every row of an ordered query, read PAGE rows at a time until a page comes back empty, so a
 * project whose max rows is below PAGE still gives them all. */
async function every<T>(page: (from: number, to: number) => Page<T>): Promise<T[]> {
  const rows: T[] = [];
  for (;;) {
    const { data, error } = await page(rows.length, rows.length + PAGE - 1);
    if (error) throw error;
    if (!data?.length) return rows;
    rows.push(...data);
  }
}

/**
 * The dashboard's data, read under row-level security: the latest game day's decisions, every
 * paper bet, the latest live report, and the CLV per bet of every report under its policy.
 * Throws Supabase's error when a read fails.
 */
export async function load(client: SupabaseClient): Promise<Data> {
  const owner = await client.rpc("is_dashboard_owner");
  if (owner.error) throw owner.error;
  if (owner.data !== true) return NOTHING;
  const [decisions, bets, reports] = await Promise.all([
    client
      .from("predictions")
      .select(PREDICTION_COLUMNS)
      .order("game_date", { ascending: false })
      .order("start_utc")
      .limit(LATEST_DECISIONS)
      .overrideTypes<Prediction[], { merge: false }>(),
    every((from, to) =>
      client
        .from("paper_bets")
        .select(BET_COLUMNS)
        .order("game_date", { ascending: false })
        .order("start_utc")
        .order("game_id")
        .range(from, to)
        .overrideTypes<Bet[], { merge: false }>(),
    ),
    client
      .from("live_reports")
      .select("as_of, kind, policy_version, report, code_version")
      .order("as_of", { ascending: false })
      .limit(1)
      .overrideTypes<ReportRow[], { merge: false }>(),
  ]);
  for (const result of [decisions, reports]) if (result.error) throw result.error;
  const latest = reports.data?.[0] ?? null;
  // A new policy version restarts the CLV count (ADR 0032), so the history is the latest
  // report's policy only, from its first report.
  const history = latest
    ? await every((from, to) =>
        client
          .from("live_reports")
          .select("as_of, clv:report->closing_value->clv_per_bet")
          .eq("policy_version", latest.policy_version)
          .order("as_of", { ascending: false })
          .range(from, to)
          .overrideTypes<Data["history"], { merge: false }>(),
      )
    : [];
  const rows = decisions.data ?? [];
  const day = rows[0]?.game_date ?? null;
  return {
    owner: true,
    day,
    slate: rows.filter((r) => r.game_date === day),
    bets,
    report: latest?.report ?? null,
    history,
    scores: await scores(client, bets),
  };
}

/** The final scores of the settled bets' games, from their first date on, or null when the
 * owner can't read games yet (the migration of #210): the results show without their scores. */
async function scores(client: SupabaseClient, bets: Bet[]): Promise<Record<number, Score> | null> {
  const settled = bets.filter((b) => b.settlement !== null);
  if (settled.length === 0) return {};
  const first = settled.reduce((d, b) => (b.game_date < d ? b.game_date : d), settled[0].game_date);
  const wanted = new Set(settled.map((b) => b.game_id));
  let games: Score[];
  try {
    games = await every((from, to) =>
      client
        .from("games")
        .select(SCORE_COLUMNS)
        .gte("game_date", first)
        .order("game_id")
        .range(from, to)
        .overrideTypes<Score[], { merge: false }>(),
    );
  } catch (error) {
    if ((error as { code?: string }).code === NOT_GRANTED) return null;
    throw error;
  }
  return Object.fromEntries(games.filter((g) => wanted.has(g.game_id)).map((g) => [g.game_id, g]));
}
