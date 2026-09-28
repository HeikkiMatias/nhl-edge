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

Option 3: `observed_utc = start_utc + 6h` (`RESULT_LAG` in `ingest/games.py`), for backfilled and live games alike. The bound errs late, so a result can only be used later than it truly became public, never earlier. The price is that a matinee result cannot feed a prediction for an evening game on the same day. That loses one game in a rolling window of dozens. `results_known_at(games, t)` is the selector, and `tests/leakage/test_games.py` checks that a game never sees its own result.

## Backtest evidence

None yet. This is a point-in-time convention, not a tuned choice. Gate 1 (B2 against B1) will be the first backtest that uses it.

## Consequences

- Every feature built from game results filters `observed_utc < prediction time`.
- The live pipeline can never be ahead of the backtest. It ingests results the next morning, at least 6 hours after the start of any game.
- The Supabase `games` table stores the same `observed_utc`.

## Revisit when

A source of true end times turns up, such as the official game summary reports. Also when a live intraday ingest makes same-day matinee results worth using.
