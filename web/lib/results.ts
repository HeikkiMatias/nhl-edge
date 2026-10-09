// The paper bets' results as the dashboard shows them during the season (ADR 0034): the bankroll
// after each settled day, its drawdown from the peak, the record and the bets per day. Every
// figure comes from the settlement's own columns (won, profit); nothing here is a verdict.

import type { Bet, Score } from "./types.ts";

/** The paper bankroll's start, in units (plan §11). */
export const START = 100;
/** The drawdown from the peak at which plan §11 calls for a review of the data and code. */
export const REVIEW_DRAWDOWN = 0.2;

/** The bankroll after a settled game day. */
export type Day = {
  date: string;
  bets: number;
  profit: number;
  balance: number;
  peak: number;
  drawdown: number;
};

/** The bankroll after each game day with a settled bet, oldest first: 100 units plus the
 * settled bets' profit to that day, with its running peak and its drawdown from it. A void bet
 * moves nothing. */
export function bankroll(bets: Bet[]): Day[] {
  const byDay = new Map<string, { bets: number; profit: number }>();
  for (const b of bets) {
    if (b.settlement !== "settled" || b.profit === null) continue;
    const day = byDay.get(b.game_date) ?? { bets: 0, profit: 0 };
    day.bets += 1;
    day.profit += b.profit;
    byDay.set(b.game_date, day);
  }
  let balance = START;
  let peak = START;
  return [...byDay.entries()]
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([date, day]) => {
      balance += day.profit;
      peak = Math.max(peak, balance);
      return { date, ...day, balance, peak, drawdown: balance / peak - 1 };
    });
}

/** The bets' record: won, lost, void and awaiting a result, the settled stakes and profit, and
 * the stakes in play on bets awaiting their result. */
export function record(bets: Bet[]) {
  const settled = bets.filter((b) => b.settlement === "settled");
  return {
    won: settled.filter((b) => b.won === true).length,
    lost: settled.filter((b) => b.won === false).length,
    void: bets.filter((b) => b.settlement === "void").length,
    open: bets.filter((b) => b.settlement === null).length,
    inPlay: bets.filter((b) => b.settlement === null).reduce((sum, b) => sum + b.stake, 0),
    staked: settled.reduce((sum, b) => sum + b.stake, 0),
    profit: settled.reduce((sum, b) => sum + (b.profit ?? 0), 0),
  };
}

/** The number of paper bets on each game day, oldest first. */
export function betsPerDay(bets: Bet[]): { date: string; bets: number }[] {
  const counts = new Map<string, number>();
  for (const b of bets) counts.set(b.game_date, (counts.get(b.game_date) ?? 0) + 1);
  return [...counts.entries()]
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([date, n]) => ({ date, bets: n }));
}

/** A bet's result in words: "Won", "Lost", "Void" or "Awaiting the result". */
export function result(bet: Bet): string {
  if (bet.settlement === "void") return "Void";
  if (bet.settlement !== "settled") return "Awaiting the result";
  return bet.won ? "Won" : "Lost";
}

/** A final score, away team first, with how it ended: "DAL 4–2 BUF", "MIN 2–3 TBL (OT)". */
export function scoreLine(bet: Bet, score: Score | undefined): string {
  if (!score) return "";
  const end = score.decided_in === "REG" ? "" : ` (${score.decided_in})`;
  return `${bet.away} ${score.away_score}–${score.home_score} ${bet.home}${end}`;
}
