// The rows the dashboard reads, as the migrations in supabase/migrations/ define them. Only the
// columns it selects: paper_bets' won and profit stay unread until the season's end (plan §11).

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

/** A paper bet with its closing line value once settled (public.paper_bets). */
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
  stake: number;
  settlement: string | null;
  close_status: string | null;
  clv: number | null;
  fair_move: number | null;
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

/** The live report (nhl live report, src/nhl_edge/live/report.py), as stored in live_reports. */
export type LiveReport = {
  as_of: string;
  kind: string;
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
    bound: Record<string, { stand_in: number; imputed: number; clv_per_bet: Figure }>;
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
