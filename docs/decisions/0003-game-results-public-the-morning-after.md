# 0003. Game results count as public at 10:00 UTC the morning after the game date

- Status: Accepted
- Date: 2026-09-28

## Context

Hard rule 1 requires every input to have an `observed_utc` before the prediction time. For `games`, the facts that matter are the final score and how the game was decided. They become public when the game ends, but the NHL API has no end-of-game time: the schedule, boxscore and play-by-play carry only the scheduled start (`startTimeUTC`). Every rolling team, goalie and rest feature built from past results depends on this timestamp, so it has to be fixed before phase 2.

Games do not always end near their scheduled time. Game 2020020287 (VGK at COL, Lake Tahoe, 2021-02-20) is listed at 20:00 UTC. It was suspended after the first period because of sun on the ice, resumed at 9 pm PT and finished at about 07:00 UTC the next day, about 11 hours after its start.

## Options

1. **Fetch time.** Use when the pipeline downloaded the result. It is correct for live ingest, but every backfilled game would count as observed in September 2026, which makes 2010 to 2026 unusable for a point-in-time backtest.
2. **Scheduled start plus six hours.** A normal game ends within about 3 hours, and one with OT and a shootout within about 3h15m. It leaks for games delayed past the bound, such as Lake Tahoe (public at 02:00 UTC, finished at about 07:00). Codex flagged this as a P0 on PR #23. It also lets a backtest use a same-day matinee result that the live pipeline only has the next morning.
3. **A fixed time the morning after the game date.** 10:00 UTC is 5 or 6 am ET. It comes after the 09:00 UTC nightly ingest and before the first odds slot (11:05 UTC in EDT, 12:05 UTC in EST).

## Decision

Option 3: `observed_utc` is 10:00 UTC on the day after `game_date`, the NHL's Eastern date (`RESULT_PUBLIC_AT` in `ingest/games.py`). The same rule applies to backfilled and live games.
- **Margins:**
  - It is at least 6.5 hours after any start (the latest starts are 10:30 pm ET, 03:30 UTC).
  - It comes 10 to 20 hours after most starts.
  - It comes about 3 hours after the Lake Tahoe game ended.
- **Schema guard:** `observed_utc` must be at least six hours after `start_utc` (`MIN_RESULT_LAG`), so an unexpectedly late start fails loudly.
- **Selector and tests:** `results_known_at(games, t)` is the selector. `tests/leakage/test_games.py` checks three things: a game never sees its own result, results are known only from the morning after, and the Lake Tahoe score stays hidden until after it ended.

## Backtest evidence

None yet. This is a point-in-time convention, not a tuned choice. Gate 1 (B2 against B1) will be the first backtest that uses it.

## Consequences

- Every feature built from game results filters `observed_utc < prediction time`.
- **Cost:** a result never feeds a prediction on its own game date. A matinee is not used for that evening's games, which loses at most one game in a rolling window of dozens.
- **Backtest and live now know the same results.** The 09:00 UTC nightly ingest has last night's results before they count, and the morning predictions can use them.
- **The rule can still be wrong in one case:** a game suspended and resumed on a later calendar day. None is known in 2010 to 2026; postponed games are listed on their rescheduled date. A known case would get an override.
- **`observed_utc` covers the result columns only.** The schedule columns in the same row (teams, start, venue) were public long before the game. Issue #24 split them out: the `schedule` table has no result columns and is public a day before the start (ADR 0005). Rest and travel features read it for the current game.
- The Supabase `games` table stores the same `observed_utc`.

## Revisit when

A source of true end times turns up, such as the official game summary reports. Also when a game is found that was suspended and resumed on a later day, or when an intraday ingest makes same-day results worth using.
