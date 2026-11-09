// When each GitHub workflow runs (issue #50). GitHub's own `schedule` started runs 3 to 6 hours
// late, while a workflow_dispatch starts within seconds, so this Worker's cron fires every five
// minutes and dispatches the workflows due at that minute. Every time below must be a multiple of
// five minutes.

// Odds slots in US Eastern wall time, so DST needs no edit (docs/data-sources.md). The workflow
// also polls pre-game goalies before each snapshot.
export const ODDS_SLOTS = {
  "07:05": "morning",
  "12:45": "midday",
  "18:45": "pre7",
  "19:45": "pre8",
  "21:45": "pre10",
};

// Goalie polls at :50 of every UTC hour from 12:50 to 02:50, for games starting within 75 minutes:
// every start from 09:00 to 23:00 ET, in EDT and EST, gets one 10 to 70 minutes before it.
export const GOALIE_HOURS_UTC = [12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 0, 1, 2];

// 04:00 or 05:00 ET, after every game of the night is final and before the 10:00 UTC at which
// results count as public (ADR 0003).
export const NIGHTLY_UTC = "09:00";

// The Kalshi watch's windows in US Eastern wall time (#319), every 4 hours: each run polls every
// game's markets until a minute before the next, inside GitHub's 6-hour job limit.
export const KALSHI_WATCH_ET = ["03:05", "07:05", "11:05", "15:05", "19:05", "23:05"];

// The Kalshi re-valuation's checks in US Eastern wall time (#321): every 15 minutes from 13:05,
// after the midday decision's orders, to 22:50, clear of the odds slots at :45, which re-value at
// each slot themselves.
export const KALSHI_REVALUE_ET = Array.from({ length: 10 }, (_, i) => 13 + i).flatMap((hour) =>
  ["05", "20", "35", "50"].map((minute) => `${hour}:${minute}`),
);

// Pinnacle about 5 minutes before each start (ADR 0060, #254), in US Eastern wall time: every 15
// minutes at :10, :25, :40 and :55 from 10:55 to 22:55, so every :00, :15, :30 and :45 start
// gets its snapshot 5 minutes before it. The job calls the Odds API only when a start is due.
export const NEAR_START_ET = [
  "10:55",
  ...Array.from({ length: 12 }, (_, i) => 11 + i).flatMap((hour) =>
    ["10", "25", "40", "55"].map((minute) => `${hour}:${minute}`),
  ),
];

const EASTERN = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/** "HH:MM" of an instant in US Eastern time. */
export function easternClock(ms) {
  const parts = EASTERN.formatToParts(new Date(ms));
  const part = (type) => parts.find((p) => p.type === type).value;
  return `${part("hour")}:${part("minute")}`;
}

/** "HH:MM" of an instant in UTC. */
export function utcClock(ms) {
  return new Date(ms).toISOString().slice(11, 16);
}

/**
 * The workflow dispatches due at a cron fire. `scheduledMs` is the fire's scheduled time; seconds
 * are dropped.
 * @returns {{workflow: string, inputs: Record<string, string>}[]}
 */
export function dueJobs(scheduledMs) {
  const ms = Math.floor(scheduledMs / 60_000) * 60_000;
  const utc = utcClock(ms);
  const jobs = [];
  const eastern = easternClock(ms);
  const slot = ODDS_SLOTS[eastern];
  if (slot) jobs.push({ workflow: "odds-snapshots.yml", inputs: { slot } });
  if (KALSHI_WATCH_ET.includes(eastern)) jobs.push({ workflow: "kalshi-watch.yml", inputs: {} });
  if (KALSHI_REVALUE_ET.includes(eastern)) {
    jobs.push({ workflow: "kalshi-revalue.yml", inputs: {} });
  }
  if (NEAR_START_ET.includes(eastern)) {
    jobs.push({ workflow: "odds-near-start.yml", inputs: {} });
  }
  const [hour, minute] = utc.split(":").map(Number);
  if (minute === 50 && GOALIE_HOURS_UTC.includes(hour)) {
    jobs.push({ workflow: "pregame-goalies.yml", inputs: {} });
  }
  if (utc === NIGHTLY_UTC) jobs.push({ workflow: "ingest-nightly.yml", inputs: {} });
  return jobs;
}
