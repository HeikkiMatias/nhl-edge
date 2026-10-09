// Formatting for the dashboard. Every figure comes from the live report (nhl live report), which
// gives a value only with its weekly block bootstrap interval, or only counts with too few weeks.

/** A report estimate: a mean with its interval, or only its counts. */
export type Estimate = {
  mean?: number;
  value?: number;
  low?: number;
  high?: number;
  games?: number;
  weeks?: number;
};

/** "+0.0123 [+0.0045, +0.0200] (120 bets, 9 weeks)", or the counts when too few weeks. */
export function estimate(figure: Estimate | null | undefined, unit = "games", digits = 4): string {
  if (!figure) return "none yet";
  const signed = (x: number) => (x >= 0 ? "+" : "") + x.toFixed(digits);
  const centre = figure.mean ?? figure.value;
  if (centre !== undefined && figure.low !== undefined && figure.high !== undefined) {
    const counts =
      figure.games !== undefined ? ` (${figure.games} ${unit}, ${figure.weeks} weeks)` : "";
    return `${signed(centre)} [${signed(figure.low)}, ${signed(figure.high)}]${counts}`;
  }
  return `${figure.games ?? 0} ${unit} over ${figure.weeks ?? 0} weeks: too few weeks for an estimate`;
}

/** A probability as a percentage with one decimal. */
export function percent(p: number | null | undefined): string {
  return p === null || p === undefined ? "" : `${(100 * p).toFixed(1)}%`;
}

/** A US Eastern calendar date (YYYY-MM-DD) of an instant: the game date the ledger keys on. */
export function easternDate(at: Date): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(at);
}

// Every clock time the dashboard shows is Helsinki time (#218). The game day stays the NHL's own
// date, which is US Eastern: a 19:00 ET game of 2026-10-08 starts at 02:00 on Friday in Helsinki.
const HELSINKI = "Europe/Helsinki";

const day = (at: Date, timeZone: string) =>
  new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(at);
const clock = (at: Date, timeZone: string) =>
  new Intl.DateTimeFormat("en-GB", { timeZone, hour: "2-digit", minute: "2-digit" }).format(at);

/** An instant as Helsinki clock time, such as "19:45", with the weekday in front when its
 * Helsinki date isn't the given game day: "Fri 02:00" for a 19:00 ET game of a Thursday. */
export function helsinkiTime(iso: string, gameDay?: string): string {
  const at = new Date(iso);
  const time = clock(at, HELSINKI);
  if (gameDay === undefined || day(at, HELSINKI) === gameDay) return time;
  const weekday = new Intl.DateTimeFormat("en-GB", { timeZone: HELSINKI, weekday: "short" }).format(at);
  return `${weekday} ${time}`;
}

/** The instant of a US Eastern clock time on a game day, such as the 12:45 ET decision (ADR
 * 0033). New York's offset is -4 or -5 hours, so of the two candidates the one that reads back
 * as that day and time is it. */
export function easternInstant(gameDay: string, time: string): Date {
  const [h, m] = time.split(":").map(Number);
  const [y, mo, d] = gameDay.split("-").map(Number);
  for (const offset of [4, 5]) {
    const at = new Date(Date.UTC(y, mo - 1, d, h + offset, m));
    if (day(at, "America/New_York") === gameDay && clock(at, "America/New_York") === time) return at;
  }
  throw new Error(`no ${time} US Eastern on ${gameDay}`);
}

/** A game date (YYYY-MM-DD) as a short label for a chart's axis, such as "Oct 8". */
export function shortDate(day: string): string {
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" }).format(
    new Date(`${day}T12:00:00Z`),
  );
}
