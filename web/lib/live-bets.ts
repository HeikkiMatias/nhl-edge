// Live games and our bets (#398 step 4): each game of the day with the bet we hold on it, what it
// pays and what it costs, and where it stands on the scoreboard right now. Display only: a live
// score never enters the record, and the official result stays the nightly settlement, on the full
// game with overtime and the shootout included (hard rule 2).

import { isFinal, type LiveGame } from "./live.ts";
import { type RealState, standing } from "./real.ts";
import type { Bet } from "./types.ts";

/** The bet a game's card shows: the one recorded on the form (real, or practice before real stakes
 * start), in euros, or else the paper bet, in units. */
export type OurBet = {
  kind: "real" | "practice" | "paper";
  game_id: number;
  side: "home" | "away";
  team: string;
  price: number;
  stake: number;
  unit: "€" | "units";
  /** A paper bet's own row, whose official settlement shows once the nightly run has it. */
  paper?: Bet;
};

/** Each game's bet on a day: the recorded entry where one stands placed, as the record applies the
 * owner's revisions and the weekly check's corrections, and the paper bet elsewhere. */
export function ourBets(day: string, paper: Bet[], real: RealState | null): Map<number, OurBet> {
  const out = new Map<number, OurBet>();
  for (const b of paper) {
    if (b.game_date !== day) continue;
    out.set(b.game_id, {
      kind: "paper",
      game_id: b.game_id,
      side: b.side,
      team: b.side === "home" ? b.home : b.away,
      price: b.price,
      stake: b.stake,
      unit: "units",
      paper: b,
    });
  }
  if (!real?.ready || real.day !== day) return out;
  for (const alert of real.alerts) {
    if (alert.status !== "alerted") continue;
    const entry = real.entries[alert.game_id] ?? null;
    const now = standing(
      entry,
      entry ? (real.revisions?.[alert.game_id] ?? []) : [],
      real.corrections[alert.game_id] ?? [],
      alert,
    );
    if (now?.outcome !== "placed" || now.matched_price === null || now.stake === null) continue;
    out.set(alert.game_id, {
      kind: alert.mode === "real" ? "real" : "practice",
      game_id: alert.game_id,
      side: alert.side,
      team: alert.side === "home" ? alert.home : alert.away,
      price: now.matched_price,
      stake: now.stake,
      unit: "€",
    });
  }
  return out;
}

/** What a bet pays if its team wins, net of the stake. */
export const pays = (b: OurBet) => b.stake * (b.price - 1);

/** A bet's state on the scoreboard: still to start, under way (leading, trailing or tied), over
 * (won or lost, provisional until the settlement, or official once settled), or not played as
 * scheduled. result is the profit once over, and null before. */
export type Now = {
  tone: "waiting" | "live" | "won" | "lost" | "off";
  text: string;
  result: number | null;
};

/** "+1.23 €", "−0.90 units". */
export function money(x: number, unit: OurBet["unit"], signed = true): string {
  const sign = x < 0 ? "−" : signed && x > 0 ? "+" : "";
  return `${sign}${Math.abs(x).toFixed(2)} ${unit}`;
}

export function rightNow(bet: OurBet, g: LiveGame | undefined): Now {
  const win = pays(bet);
  const settled = bet.paper?.settlement ?? null;
  // A paper bet the nightly run has settled: its official result, whatever the scoreboard says.
  if (bet.paper && settled !== null) {
    if (settled === "void") return { tone: "off", text: "Void: the stake is returned", result: 0 };
    const profit = bet.paper.profit ?? (bet.paper.won ? win : -bet.stake);
    return bet.paper.won
      ? { tone: "won", text: `Won ${money(profit, bet.unit)}, settled`, result: profit }
      : { tone: "lost", text: `Lost ${money(profit, bet.unit)}, settled`, result: profit };
  }
  if (!g) return { tone: "waiting", text: "Not on the scoreboard yet", result: null };
  if (g.schedule_state !== "OK") {
    const what = { PPD: "Postponed", SUSP: "Suspended", CNCL: "Cancelled" }[g.schedule_state];
    return {
      tone: "off",
      text: `${what ?? "Not played as scheduled"}: the nightly settlement decides`,
      result: null,
    };
  }
  const scored = g.home_score !== null && g.away_score !== null;
  const ours = scored ? (bet.side === "home" ? g.home_score : g.away_score) : null;
  const theirs = scored ? (bet.side === "home" ? g.away_score : g.home_score) : null;
  if (isFinal(g) && ours !== null && theirs !== null) {
    // The full game: the scoreboard credits a shootout's winner with one goal.
    return ours > theirs
      ? { tone: "won", text: `Won ${money(win, bet.unit)}, provisional until tonight's settlement`, result: win }
      : { tone: "lost", text: `Lost ${money(-bet.stake, bet.unit)}, provisional until tonight's settlement`, result: -bet.stake };
  }
  if (g.state === "LIVE" || g.state === "CRIT") {
    if (ours === null || theirs === null) return { tone: "live", text: "Under way", result: null };
    const score = `${ours}–${theirs}`;
    if (ours > theirs) {
      return { tone: "live", text: `Leading ${score}: would win ${money(win, bet.unit)} if it ended now`, result: null };
    }
    if (ours < theirs) {
      return { tone: "live", text: `Trailing ${score}: would lose ${money(-bet.stake, bet.unit)} if it ended now`, result: null };
    }
    return { tone: "live", text: `Tied ${score}: overtime or a shootout would decide`, result: null };
  }
  return { tone: "waiting", text: "Not started yet", result: null };
}

/** The day's totals over the bets in one unit: the recorded bets in euros where any are recorded,
 * else the paper bets in units. In play: the stakes of games not over. The best and worst case add
 * every open bet winning or losing to the finished ones. */
export type Totals = {
  unit: OurBet["unit"];
  kind: OurBet["kind"];
  bets: number;
  inPlay: number;
  best: number;
  worst: number;
  finished: number;
  over: number;
};

export function totals(bets: OurBet[], games: ReadonlyMap<number, LiveGame>): Totals | null {
  const recorded = bets.filter((b) => b.unit === "€");
  const counted = recorded.length ? recorded : bets;
  if (counted.length === 0) return null;
  let inPlay = 0;
  let best = 0;
  let worst = 0;
  let finished = 0;
  let over = 0;
  for (const b of counted) {
    const now = rightNow(b, games.get(b.game_id));
    if (now.result !== null) {
      finished += now.result;
      over += 1;
    } else {
      // Still to be decided, a postponed game too: its stake stays at risk.
      inPlay += b.stake;
      best += pays(b);
      worst -= b.stake;
    }
  }
  return {
    unit: counted[0].unit,
    kind: recorded.length ? (recorded.some((b) => b.kind === "real") ? "real" : "practice") : "paper",
    bets: counted.length,
    inPlay,
    best: finished + best,
    worst: finished + worst,
    finished,
    over,
  };
}
