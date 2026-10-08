import type { SupabaseClient } from "@supabase/supabase-js";

import type { Bet, Figure, LiveReport, Prediction, ReportRow } from "./types.ts";

export const PREDICTION_COLUMNS =
  "game_date, game_id, start_utc, home, away, status, home_price, away_price, p_b1, p_b3, " +
  "p_blend, bet";
// Never won or profit: no result-based figure shows before the season's end (plan §11).
export const BET_COLUMNS =
  "game_date, game_id, start_utc, home, away, side, price, p_side, ev, stake, settlement, " +
  "close_status, clv, fair_move";
// A day's slate is at most 16 games, so the latest 40 decisions hold the latest day's.
const LATEST_DECISIONS = 40;
const BETS_SHOWN = 200;
// The nightly reports' CLV per bet to date, newest first.
const CLV_HISTORY = 60;

/** What the dashboard shows: nothing for a signed-in user who isn't an owner. */
export type Data = {
  owner: boolean;
  day: string | null;
  slate: Prediction[];
  bets: Bet[];
  report: LiveReport | null;
  history: { as_of: string; clv: Figure }[];
};

const NOTHING: Data = {
  owner: false,
  day: null,
  slate: [],
  bets: [],
  report: null,
  history: [],
};

/**
 * The dashboard's data, read under row-level security: the latest game day's decisions, the
 * paper bets, the latest live report, and the CLV per bet of each earlier report under the same
 * policy. Throws Supabase's error when a read fails.
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
    client
      .from("paper_bets")
      .select(BET_COLUMNS)
      .order("game_date", { ascending: false })
      .order("start_utc")
      .limit(BETS_SHOWN)
      .overrideTypes<Bet[], { merge: false }>(),
    client
      .from("live_reports")
      .select("as_of, kind, policy_version, report, code_version")
      .order("as_of", { ascending: false })
      .limit(1)
      .overrideTypes<ReportRow[], { merge: false }>(),
  ]);
  for (const result of [decisions, bets, reports]) if (result.error) throw result.error;
  const latest = reports.data?.[0] ?? null;
  // A new policy version restarts the CLV count (ADR 0032), so the history is the latest
  // report's policy only.
  let history: Data["history"] = [];
  if (latest) {
    const earlier = await client
      .from("live_reports")
      .select("as_of, clv:report->closing_value->clv_per_bet")
      .eq("policy_version", latest.policy_version)
      .order("as_of", { ascending: false })
      .limit(CLV_HISTORY)
      .overrideTypes<{ as_of: string; clv: Figure }[], { merge: false }>();
    if (earlier.error) throw earlier.error;
    history = earlier.data ?? [];
  }
  const rows = decisions.data ?? [];
  const day = rows[0]?.game_date ?? null;
  return {
    owner: true,
    day,
    slate: rows.filter((r) => r.game_date === day),
    bets: bets.data ?? [],
    report: latest?.report ?? null,
    history,
  };
}
