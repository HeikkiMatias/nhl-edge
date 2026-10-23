// The rows the dashboard reads, as the migrations in supabase/migrations/ define them. Only the
// columns it selects. Each bet's result and profit show during the season (ADR 0034).

/** A slate game's paper decision (public.predictions). */
export type Prediction = {
  game_date: string;
  game_id: number;
  start_utc: string;
  home: string;
  away: string;
  status: string;
  home_price: number | null;
  away_price: number | null;
  p_b1: number | null;
  p_b3: number | null;
  p_blend: number | null;
  bet: boolean;
};

/** A paper bet with its result, profit and closing line value once settled (public.paper_bets).
 * The result is on the full game, overtime and the shootout included. */
export type Bet = {
  game_date: string;
  game_id: number;
  start_utc: string;
  home: string;
  away: string;
  side: "home" | "away";
  price: number;
  p_side: number;
  ev: number;
  bankroll: number;
  stake: number;
  settled_utc: string | null;
  settlement: string | null;
  won: boolean | null;
  profit: number | null;
  close_status: string | null;
  clv: number | null;
  fair_move: number | null;
  /** The frozen policy that placed the bet: v1 to 2026-10-14, v2 from 10-15 (ADR 0037). */
  policy_version: string;
};

/** A game's final score (public.games): full-game, so a shootout adds one goal for its winner. */
export type Score = {
  game_id: number;
  home_score: number;
  away_score: number;
  decided_in: "REG" | "OT" | "SO";
};

/** A figure of the live report: a mean or value with its interval, or only its counts. */
export type Figure = {
  mean?: number;
  value?: number;
  low?: number;
  high?: number;
  games?: number;
  weeks?: number;
};

/** ADR 0039's start rule and weekly CLV stop as a live report states them (live/real.py, #267):
 * each condition with whether it holds, the two signals that block a start, and the verdict. */
export type RealStakes = {
  window: [string, string];
  weekly: boolean;
  verdict: string;
  failing: string[];
  conditions: {
    a: {
      measured: boolean;
      holds: boolean;
      /** CLV per bet with its weekly block bootstrap interval, or only its counts. */
      clv_per_bet: Figure;
      /** The pessimistic bound's CLV per bet, likewise; null without an eligible bet. */
      bound: Figure | null;
      share_with_proxy: number | null;
      floor: number;
    };
    c: { holds: boolean; undecided_days: string[]; unfinished_bundles: string[] | null };
    d: { holds: boolean; venue: string | null };
  };
  veto: { paper_bankroll: number; start: number; applies: boolean };
  drawdown_review: { applies: boolean };
  clv_stop: { measured: boolean; weekly: boolean; stop: boolean | null; pause: boolean | null };
};

/** The live report (nhl live report, src/nhl_edge/live/report.py), as stored in live_reports. */
export type LiveReport = {
  as_of: string;
  kind: string;
  /** The frozen policy's real-stakes section; absent from an earlier policy's report. */
  real_stakes?: RealStakes | null;
  policy_version: string;
  blend_versions: string[];
  review_date: string;
  coverage: {
    slate_games: number;
    predicted: number;
    not_predicted: Record<string, number>;
    awaiting_result: number;
    bets: number;
    awaiting_settlement: number;
    void_postponed: number;
    settled: number;
    no_pregame_snapshot: number;
    eligible: number;
    with_proxy: number;
    eligible_without_proxy: Record<string, number>;
    share_with_proxy: number | null;
  };
  closing_value: {
    clv_per_bet: Figure;
    clv_stake_weighted: Figure | null;
    fair_move_per_bet: Figure;
    floor: { required: number; share_with_proxy: number | null };
    bound: Record<string, { stand_in: number | null; imputed: number; clv_per_bet: Figure }>;
  };
  comparisons: Record<string, { difference: Figure; left_out: number }>;
  calibration: {
    games: number;
    weeks: number;
    intercept?: Figure;
    slope?: Figure;
    at?: { forecast: number; band: [number, number]; recalibrated: Figure }[];
  };
  gaps: {
    game_date: string;
    game_id: number;
    away: string;
    home: string;
    p_b1: number;
    p_b3: number | null;
    p_blend: number;
    gap: number;
    bet: boolean;
    side: string | null;
  }[];
  alerts: {
    operations: {
      days: number;
      days_skipped: number;
      no_price: number;
      stale_price: number;
      missing_input: number;
      picked: number;
      guarded: number;
    };
    drawdown: {
      threshold: number;
      triggered: boolean;
      first_utc: string | null;
      max_drawdown?: number;
    };
    u_range: Record<string, number>;
  };
  verdicts: { closing_value: string; calibration: string; incomplete: Record<string, number> };
  returns?: { bets: number; staked: number; profit: number; return_per_bet: Figure };
};

/** A row of public.live_reports. */
export type ReportRow = {
  as_of: string;
  kind: string;
  policy_version: string;
  report: LiveReport;
  code_version: string;
};

/** A paper bet of a day as alerted for a real stake, or a practice one before the start
 * (public.real_alerts, #268, ADR 0039). status is "alerted", or why it was skipped. stakes lists
 * the stake in euros at each price from min_price up to the paper price; above it the stake is
 * max_stake. */
export type RealAlert = {
  game_date: string;
  game_id: number;
  start_utc: string;
  home: string;
  away: string;
  mode: "practice" | "real" | "paused" | "stopped" | "closed";
  venue: string;
  status: string;
  side: "home" | "away";
  paper_price: number;
  min_price: number;
  max_stake: number;
  bankroll: number;
  stakes: { price: number; stake: number }[];
  alert_utc: string;
  /** On an exchange venue, the bet's buy order (ADR 0040, #287); absent before the Kalshi
   * migration, null on any other venue. */
  exchange?: RealOrder | null;
};

/** An alerted bet's buy order on Kalshi, placed automatically (ADR 0040): at most limit dollars
 * a contract, for contracts, costing at most max_cost_usd with the fee (stake euros at
 * usd_per_eur), resting until expires_utc, 5 minutes before the start. From ADR 0045 (#321,
 * execution "adr-0045") those are its taker part's, bought at once, and maker is the post-only
 * bid for the rest, both parts at most its max_cost_usd; null when its stake buys no contract. */
export type RealOrder = {
  env: "live" | "demo";
  execution?: "adr-0045";
  limit: string;
  contracts: string;
  max_cost_usd: string;
  stake: number;
  effective: number;
  maker?: RealOrderPart | null;
  usd_per_eur: number;
  expires_utc: string;
};

/** An order part's limit in dollars, contracts, most cost in USD, stake in euros and effective
 * decimal price. */
export type RealOrderPart = {
  limit: string;
  contracts: string;
  max_cost_usd: string;
  stake: number;
  effective: number;
};

/** What the owner did with an alerted bet, from the form (public.real_entries), or an automatic
 * Kalshi bet's entry from its fills (source "kalshi", #287). late is whether it was recorded at or
 * after the game's start; the database sets it and the entry time for the form's. */
export type RealEntry = {
  game_date: string;
  game_id: number;
  outcome: "placed" | "not placed";
  matched_price: number | null;
  stake: number | null;
  bet_id: string | null;
  placed_utc: string | null;
  reason: string | null;
  entered_utc: string;
  late: boolean;
  /** Absent before the Kalshi migration, when every entry is the form's. */
  source?: "form" | "kalshi";
};

/** A settlement revision of a placed real or practice bet (public.real_settlements, #270): on the
 * full game at the matched price and stake placed, or, for a Kalshi bet held for Kalshi's own
 * resolution, a payout at its price with no result (#290). The latest revision of each bet counts. */
export type RealSettlement = {
  game_date: string;
  game_id: number;
  revision: number;
  mode: "practice" | "real";
  side: "home" | "away";
  matched_price: number;
  stake: number;
  status: "settled" | "void" | "cancelled" | "payout";
  won: boolean | null;
  profit: number;
  result_utc: string;
  bankroll_after: number;
};

/** A correction to a bet's entry (public.real_corrections, #270), applied in order over
 * real_entries, as the R2 record's real_bets applies it. */
export type RealCorrection = {
  game_date: string;
  game_id: number;
  n: number;
  changes: Partial<Pick<RealEntry, "outcome" | "matched_price" | "stake" | "bet_id" | "placed_utc" | "reason">>;
  reason: string;
  corrected_utc: string;
};

/** An alerted day's mode (public.real_days, #270): a day with no bets too, so a start, pause,
 * stop or close on such a day is on record. */
export type RealDayMode = {
  game_date: string;
  mode: RealAlert["mode"];
  started: string | null;
  reason: string;
};
