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

/** A UTC timestamp as US Eastern clock time, such as "19:00". */
export function easternTime(iso: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(iso));
}

/** A game date (YYYY-MM-DD) as a short label for a chart's axis, such as "Oct 8". */
export function shortDate(day: string): string {
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" }).format(
    new Date(`${day}T12:00:00Z`),
  );
}
