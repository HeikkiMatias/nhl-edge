// SYNTHETIC quote_live rows for the tests (#398 step 3): made-up prices in the shape of
// quotes_live.snapshot, never Kalshi's own (#280).
import type { LiveRow } from "../live-quotes.ts";

export function liveRow(changes: Partial<LiveRow> = {}): LiveRow {
  return {
    game_date: "2026-11-05",
    variant: "model-1c",
    half_spread_cents: 1,
    game_id: 2026020160,
    team: "BOS",
    home: true,
    opponent: "TOR",
    start_utc: "2026-11-06T00:00:00Z",
    state: "quoting",
    fair: 0.55,
    yes_bid: 0.52,
    yes_ask: 0.54,
    top_utc: "2026-11-05T22:00:00Z",
    bid_price: 0.53,
    bid_left: 10,
    contracts: 10,
    cost_usd: 5.34,
    fills: 1,
    updated_utc: "2026-11-05T22:01:00Z",
    ...changes,
  };
}

/** Two games at 1¢: BOS-TOR quoting with 10 on BOS and 4 on TOR, SEA-VGK pulled with nothing. */
export function liveRows(): LiveRow[] {
  return [
    liveRow(),
    liveRow({ team: "TOR", home: false, opponent: "BOS", fair: 0.45, yes_bid: 0.45, yes_ask: 0.47,
      bid_price: 0.44, contracts: 4, cost_usd: 1.8 }),
    liveRow({ game_id: 2026020161, team: "VGK", opponent: "SEA", start_utc: "2026-11-06T03:00:00Z",
      state: "pulled_move", bid_price: null, bid_left: null, contracts: 0, cost_usd: 0, fills: 0 }),
    liveRow({ game_id: 2026020161, team: "SEA", home: false, opponent: "VGK",
      start_utc: "2026-11-06T03:00:00Z", state: "pulled_move", fair: 0.4, bid_price: null,
      bid_left: null, contracts: 0, cost_usd: 0, fills: 0 }),
    // The same game at 2¢: another variant's rows, never mixed in.
    liveRow({ variant: "model-2c", half_spread_cents: 2, contracts: 0, cost_usd: 0, fills: 0 }),
  ];
}
