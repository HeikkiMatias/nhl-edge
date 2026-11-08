// SYNTHETIC quote tables for the tests (#398): made-up numbers in the shape of
// kalshi_quotes.day_figures and summary, never the simulator's own figures (#280).
import type { QuoteDay, QuoteReport } from "../quotes.ts";

const BASE: QuoteDay = {
  game_date: "2026-10-31",
  record: "kalshi_quotes/1",
  variant: "model-1c",
  fair: "model",
  half_spread_cents: 1,
  hold: null,
  games: 4,
  watch_logs: true,
  posted: 20,
  pulled_move: 2,
  pulled_starter: 1,
  fills: 2,
  contracts: 20,
  matched_contracts: 5,
  largest_inventory: 15,
  cost_usd: 10,
  fees_usd: 0.1,
  edge_fair_usd: 0.3,
  last_contracts: 20,
  edge_last_usd: 0.1,
  close_contracts: 10,
  edge_close_usd: -0.2,
  settled_contracts: 20,
  payout_usd: 12,
  profit_usd: 2,
  quoted_minutes: null,
  late_minutes: null,
};

export function quoteDay(changes: Partial<QuoteDay> = {}): QuoteDay {
  const fair = changes.fair ?? BASE.fair;
  const cents = changes.half_spread_cents ?? BASE.half_spread_cents;
  const hold = changes.hold ?? null;
  return {
    ...BASE,
    variant: `${fair}-${cents}c${hold ? `-${hold}` : ""}`,
    ...changes,
  };
}

const DATES = ["2026-10-31", "2026-11-01", "2026-11-02"];

/** Three game days of the simulator's record, for the model and Pinnacle at each half-spread:
 * the model at 1¢ wins $2, loses $3, then waits for its results. */
export function quoteDays(): QuoteDay[] {
  const rows: QuoteDay[] = [];
  for (const fair of ["model", "pinnacle"]) {
    for (const cents of [0, 1, 2]) {
      DATES.forEach((game_date, i) => {
        const profit = i === 2 ? null : i === 0 ? 2 : -3;
        rows.push(
          quoteDay({
            game_date,
            fair,
            half_spread_cents: cents,
            profit_usd: fair === "model" && cents === 1 ? profit : 0.5,
            settled_contracts: i === 2 ? 0 : 20,
            payout_usd: i === 2 ? 0 : 12,
          }),
        );
      });
    }
  }
  for (const hold of ["wait", "cool", "return", "trust"]) {
    for (const cents of [0, 1, 2]) {
      rows.push(
        quoteDay({
          game_date: "2026-11-04",
          record: "kalshi_quotes/holds/1",
          hold,
          half_spread_cents: cents,
          quoted_minutes: 120,
          late_minutes: 45,
          profit_usd: hold === "trust" ? -1 : 1,
        }),
      );
    }
  }
  for (const hold of ["wait", "requote"]) {
    for (const cents of [0, 1, 2]) {
      rows.push(
        quoteDay({
          game_date: "2026-11-09",
          record: "kalshi_quotes/requote/1",
          hold,
          half_spread_cents: cents,
          quoted_minutes: hold === "requote" ? 150 : 90,
          late_minutes: 40,
          profit_usd: hold === "requote" ? 1.5 : 0.25,
        }),
      );
    }
  }
  return rows;
}

export function quoteReports(withIntervals = false): QuoteReport[] {
  const spread = withIntervals
    ? { mean: 0.015, low: 0.01, high: 0.02, games: 120, weeks: 4 }
    : { games: 6, weeks: 1 };
  const close = withIntervals
    ? { mean: -0.012, low: -0.03, high: 0.004, games: 60, weeks: 4 }
    : { games: 3, weeks: 1 };
  return [
    {
      as_of: "2026-11-03",
      record: "kalshi_quotes/1",
      figures: {
        days: 3,
        days_with_watch_logs: 3,
        "model-1c": {
          posted: 60,
          pulled_starter: 3,
          pulled_move: 6,
          fills: 6,
          contracts: 60,
          matched_contracts: 15,
          largest_inventory: 15,
          profit_usd: -1,
          spread,
          markout_5m: spread,
          markout_30m: spread,
          markout_last: spread,
          markout_close: close,
          profit: spread,
        },
      },
    },
    {
      as_of: "2026-11-05",
      record: "kalshi_quotes/holds/1",
      figures: {
        days: 1,
        days_with_watch_logs: 1,
        against_wait: {
          "model-1c-trust": withIntervals
            ? { mean: -0.4, low: -0.9, high: 0.1, games: 50, weeks: 4 }
            : { games: 4, weeks: 1 },
          "model-1c-cool": { games: 4, weeks: 1 },
        },
      },
    },
    {
      as_of: "2026-11-10",
      record: "kalshi_quotes/requote/1",
      figures: {
        days: 1,
        days_with_watch_logs: 1,
        changes: { changes: 5, requoted: 2, late: 2, withdrawn: 1 },
        against_wait: {
          "model-1c-requote": withIntervals
            ? { mean: 0.3, low: -0.2, high: 0.8, games: 80, weeks: 4 }
            : { games: 3, weeks: 1 },
        },
      },
    },
  ];
}
