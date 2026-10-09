// Live scores for the slate's games (#211): the NHL's public scoreboard, read through the app's own
// route (app/api/scores), since api-web.nhle.com sends no CORS header. Display only: a live score
// never enters the ledger, the settlement, CLV or any model input. The official result stays the
// nightly settlement, on the full game with overtime and the shootout included (hard rule 2).

import { easternDate } from "./format.ts";
import type { Bet } from "./types.ts";

export const NHL_SCORES = "https://api-web.nhle.com/v1/score";
/** How long the app's route lets a scoreboard answer be reused: many open pages make one request. */
export const CACHE_SECONDS = 20;
/** The refresh while a game is on or about to start, and otherwise while a game is still to come. */
export const LIVE_POLL_MS = 30_000;
export const IDLE_POLL_MS = 300_000;
/** Games starting within this long count as about to start. */
const SOON_MS = 30 * 60_000;
/** The route serves dates this many days either side of today's US Eastern date, no further. */
export const MAX_DAYS_AWAY = 2;

export type PeriodType = "REG" | "OT" | "SO";

/** A game on the scoreboard. state is the NHL's: FUT, PRE, LIVE, CRIT, FINAL or OFF (official). */
export type LiveGame = {
  game_id: number;
  start_utc: string;
  state: string;
  schedule_state: string;
  away: string;
  home: string;
  away_score: number | null;
  home_score: number | null;
  period: number | null;
  period_type: PeriodType | null;
  clock: string | null;
  intermission: boolean;
  last_period_type: PeriodType | null;
};

export type Scores = { date: string; fetched_utc: string; games: LiveGame[] };

type Raw = {
  games?: {
    id: number;
    startTimeUTC: string;
    gameState: string;
    gameScheduleState?: string;
    awayTeam: { abbrev: string; score?: number };
    homeTeam: { abbrev: string; score?: number };
    period?: number;
    periodDescriptor?: { periodType?: string };
    clock?: { timeRemaining?: string; inIntermission?: boolean };
    gameOutcome?: { lastPeriodType?: string };
  }[];
};

/** REG, OT or SO, and null for anything else the scoreboard might send. */
function periodType(value: string | undefined): PeriodType | null {
  return value === "REG" || value === "OT" || value === "SO" ? value : null;
}

/** The scoreboard's games, with only the fields the board shows. */
export function parseScores(date: string, raw: Raw, now: Date): Scores {
  return {
    date,
    fetched_utc: now.toISOString(),
    games: (raw.games ?? []).map((g) => ({
      game_id: g.id,
      start_utc: g.startTimeUTC,
      state: g.gameState,
      schedule_state: g.gameScheduleState ?? "OK",
      away: g.awayTeam.abbrev,
      home: g.homeTeam.abbrev,
      away_score: g.awayTeam.score ?? null,
      home_score: g.homeTeam.score ?? null,
      period: g.period ?? null,
      period_type: periodType(g.periodDescriptor?.periodType),
      clock: g.clock?.timeRemaining ?? null,
      intermission: g.clock?.inIntermission ?? false,
      last_period_type: periodType(g.gameOutcome?.lastPeriodType),
    })),
  };
}

/** Whether the route serves a date: YYYY-MM-DD, a real day, and at most MAX_DAYS_AWAY days from
 * today's US Eastern date, so the route is no general proxy for the NHL's archive. */
export function dateAllowed(date: string, today: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return false;
  const day = Date.parse(`${date}T00:00:00Z`);
  if (Number.isNaN(day) || new Date(day).toISOString().slice(0, 10) !== date) return false;
  return Math.abs(day - Date.parse(`${today}T00:00:00Z`)) <= MAX_DAYS_AWAY * 86_400_000;
}

/** Whether a slate's day is today's or yesterday's US Eastern date: a late game of yesterday's
 * slate is still on after midnight ET, and older days have nothing live. Yesterday is the
 * calendar day before today's ET date, never 24 hours ago, which misses it across a DST change. */
export function isCurrent(day: string, now: Date): boolean {
  const today = easternDate(now);
  const before = new Date(`${today}T12:00:00Z`);
  before.setUTCDate(before.getUTCDate() - 1);
  return day === today || day === before.toISOString().slice(0, 10);
}

export const isFinal = (g: LiveGame) => g.state === "FINAL" || g.state === "OFF";
const isOn = (g: LiveGame) => g.state === "LIVE" || g.state === "CRIT" || g.state === "PRE";
/** Not played as scheduled: postponed, suspended or cancelled. */
const isOff = (g: LiveGame) => g.schedule_state !== "OK";
/** Postponed or cancelled: nothing more will happen on this date. A suspended game may resume. */
const isOver = (g: LiveGame) => g.schedule_state === "PPD" || g.schedule_state === "CNCL";

const ORDINAL = ["", "1st", "2nd", "3rd"];

/** A game's state in words: "Starts 19:00 ET", "2nd · 12:34", "2nd intermission", "OT · 3:12",
 * "Shootout", "Final", "Final (OT)", "Final (SO)" or "Postponed". */
export function statusLine(g: LiveGame, startEt: string): string {
  if (g.schedule_state === "PPD") return "Postponed";
  if (g.schedule_state === "SUSP") return "Suspended";
  if (g.schedule_state === "CNCL") return "Cancelled";
  if (isFinal(g)) {
    const end = g.last_period_type === "OT" || g.last_period_type === "SO";
    return end ? `Final (${g.last_period_type})` : "Final";
  }
  if (g.state === "FUT") return `Starts ${startEt} ET`;
  if (g.state === "PRE") return "Warm-up";
  const name =
    g.period_type === "OT" ? "OT" : g.period_type === "SO" ? "Shootout" : ORDINAL[g.period ?? 0];
  if (g.period_type === "SO") return name;
  if (g.intermission) return `${name ?? "Period"} intermission`;
  return g.clock ? `${name} · ${g.clock}` : (name ?? g.state);
}

/** The bet's provisional result once its game is over on the scoreboard: the full game, so the
 * shootout's winner (credited one goal) wins. Null until then, and for a game not played. */
export function provisional(bet: Bet, g: LiveGame | undefined): "won" | "lost" | null {
  if (!g || !isFinal(g) || isOff(g) || g.home_score === null || g.away_score === null) return null;
  return (bet.side === "home") === g.home_score > g.away_score ? "won" : "lost";
}

/** When to read the scoreboard again: every LIVE_POLL_MS while a game is on or starts within
 * half an hour, every IDLE_POLL_MS while one is still to come or suspended, and never once all
 * are over (final, postponed or cancelled). */
export function nextPoll(games: LiveGame[], now: Date): number | null {
  const open = games.filter((g) => !isFinal(g) && !isOver(g));
  if (open.length === 0) return null;
  const soon = open.some(
    (g) =>
      !isOff(g) &&
      (isOn(g) || (g.state === "FUT" && Date.parse(g.start_utc) - now.getTime() <= SOON_MS)),
  );
  return soon ? LIVE_POLL_MS : IDLE_POLL_MS;
}
