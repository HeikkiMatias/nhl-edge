// The paper bets' results as the dashboard shows them during the season (ADR 0034): the bankroll
// after each settled day, its drawdown from the peak, the record and the bets per day. Every
// figure comes from the settlement's own columns (won, profit); nothing here is a verdict.

import type { Bet, Score } from "./types.ts";

/** The paper bankroll's start, in units (plan §11). */
export const START = 100;
/** The drawdown from the peak at which plan §11 calls for a review of the data and code. */
export const REVIEW_DRAWDOWN = 0.2;

/** The bankroll after a settled game day, and the policy that placed its bets. */
export type Day = {
  date: string;
  bets: number;
  profit: number;
  balance: number;
  peak: number;
  drawdown: number;
  policy: string;
};

/** The bankroll after each game day with a settled bet, oldest first: 100 units plus the
 * settled bets' profit to that day, with its running peak and its drawdown from it. A void bet
 * moves nothing.
 *
 * Game days come in the order their results became public, as the live report applies profits
 * (by result_utc, its game's start plus six hours): a bet is decided at 12:45 ET on its date, and a
 * game played more than 12 hours from its ledger start is void (settle.POSTPONED), so one day's
 * results are all public before the next day's. The report's drawdown alert, which steps by each
 * result rather than by day, stays the one that triggers the review. */
export function bankroll(bets: Bet[]): Day[] {
  const byDay = new Map<string, { bets: number; profit: number; policy: string }>();
  for (const b of bets) {
    if (b.settlement !== "settled" || b.profit === null) continue;
    // A day's bets are one decision, so one policy's.
    const day = byDay.get(b.game_date) ?? { bets: 0, profit: 0, policy: b.policy_version };
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

/** Each policy's short name, v1, v2 and on, in the order of its first bet. One paper bankroll
 * runs through them all: a new policy carries on the earlier one's balance (ADR 0037). */
export function policyNames(bets: Bet[]): Map<string, string> {
  const first = new Map<string, string>();
  for (const b of bets) {
    const seen = first.get(b.policy_version);
    if (seen === undefined || b.game_date < seen) first.set(b.policy_version, b.game_date);
  }
  const order = [...first.entries()].sort(([, a], [, b]) => (a < b ? -1 : a > b ? 1 : 0));
  return new Map(order.map(([version], i) => [version, `v${i + 1}`]));
}

/** The settled days where a new policy's bets begin, by their index in days. */
export function policyStarts(days: Day[]): number[] {
  return days.flatMap((d, i) => (i > 0 && d.policy !== days[i - 1].policy ? [i] : []));
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
