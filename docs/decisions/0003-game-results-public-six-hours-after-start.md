# 0003. Game results count as public six hours after the scheduled start

- Status: Accepted
- Date: 2026-09-28

## Context

Hard rule 1 requires every input to have an `observed_utc` before the prediction time. For `games`, the facts that matter are the final score and how the game was decided. They become public when the game ends, but the NHL API has no end-of-game time: the schedule, boxscore and play-by-play carry only the scheduled start (`startTimeUTC`). Every rolling team, goalie and rest feature built from past results depends on this timestamp, so it has to be fixed before phase 2.

## Options

1. **Fetch time.** Use when the pipeline downloaded the result. It is correct for live ingest, but every backfilled game would count as observed in September 2026, which makes 2010 to 2026 unusable for a point-in-time backtest.
2. **The next nightly ingest (09:00 UTC).** This matches what the live pipeline knows before its daily run. It ties a data fact to one job's schedule, and any intraday ingest added later would be understated.
3. **Scheduled start plus a fixed bound.** A regular-season game ends within about 3 hours of its scheduled start, and one with OT and a shootout within about 3h15m. A six-hour bound covers long delays too.

## Decision

Option 3: `observed_utc = start_utc + 6h` (`RESULT_LAG` in `ingest/games.py`), for backfilled and live games alike. The bound errs late, so a result can only be used later than it truly became public, never earlier. The price is that a game started less than six hours before a prediction is left out, even when it has already ended. That loses at most one game in a rolling window of dozens. `results_known_at(games, t)` is the selector, and `tests/leakage/test_games.py` checks that a game never sees its own result.

## Backtest evidence

None yet. This is a point-in-time convention, not a tuned choice. Gate 1 (B2 against B1) will be the first backtest that uses it.

## Consequences

- Every feature built from game results filters `observed_utc < prediction time`.
- The backtest can be slightly ahead of live, though this is not leakage.
  - The live pipeline ingests results in the 09:00 UTC nightly run.
  - A backtest prediction can use a same-day result once its six hours have passed. For example, a noon ET start becomes usable before a 10 pm ET puck drop.
  - The result was public by then, so nothing leaks. It only reaches league-wide components, because a team never plays twice in a day.
  - An intraday ingest before the evening odds slots would close the gap.
- `observed_utc` covers the result columns only. The schedule columns in the same row (teams, start, venue) were public long before the game. Issue #24 splits schedule from results before phase 2, so rest and travel features can read the current game's schedule without its result.
- The Supabase `games` table stores the same `observed_utc`.

## Revisit when

A source of true end times turns up, such as the official game summary reports. Also when a live intraday ingest makes same-day matinee results worth using.
