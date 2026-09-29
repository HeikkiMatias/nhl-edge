# 0005. A game's schedule counts as public a day before it starts

- Status: Accepted
- Date: 2026-09-29

## Context

Rest, travel and home-ice features (B2's ΔR and h_s) need the pre-game facts of the game being predicted: its teams, start, venue, and the neutral-site and limited-attendance flags. Until now these lived only in `games`, next to the result. Their only timestamp was the result's (10:00 UTC the morning after, ADR 0003), so `results_known_at` rightly hides the whole row on game day. A feature author would have had to bypass `observed_utc` to get the schedule, and would take the score along. The leakage audit of #23 flagged this (#24).

The schedule needs its own timestamp. The API doesn't say when a game was scheduled. Every backfilled row was fetched in September 2026.

## Options

1. **Fetch time.** It is exact for live, but it puts every backfilled row in 2026, the reason ADR 0003 rejected it for results.
2. **The season's schedule release date.** The regular-season schedule comes out each summer, but there is no reliable source of the dates. It is also wrong for postponed games, which got their new dates later.
3. **A fixed lead: 24 hours before `start_utc`.** It is conservative for games published months ahead, and it holds for postponed games, which the NHL re-dated days or weeks ahead. It is the same rule for backfilled and live rows.
4. **A longer lead, such as a week.** It adds nothing a current-game feature needs, and it would be wrong for a game re-dated at short notice.

## Decision

Option 3. `schedule.observed_utc = start_utc − 24 h`, set in `schedule_of` (`ingest/games.py`) from `SCHEDULE_LEAD`. A game known to have been re-timed at shorter notice gets a later time from `SCHEDULE_PUBLIC_OVERRIDES`: the latest moment the change can have become public. The schema rejects an `observed_utc` earlier than a day before the start, which would leak. It allows a later one before the start, for an override. No point-in-time schedule snapshots exist for 2010-26, so the rule plus overrides for known cases is as exact as the data allows.

- **The schedule is its own table.** `schedule` holds the pre-game columns of each final regular-season game and no result columns, so reading it can never reveal a score.
- **`games` is unchanged.** It keeps its result timestamp and its teams and date, so results read on their own.
- **Game-day predictions see the schedule.** The morning odds slot (11:05 UTC) and the 16:00 UTC run both come after every same-day game's schedule is public, even for a 16:00 UTC European start, public at 16:00 UTC the day before.

## Backtest evidence

None yet. This is a point-in-time convention, not a tuned choice. The first run that relies on it is gate 1's B2 against B1 with rest and travel features.

## Consequences

- **Where features read.** Features read `schedule` through `schedule_known_at(schedule, prediction_utc, predicting)`: the games already played, whose result is public by the prediction time (ADR 0003), plus the games being predicted once public. Anything about outcomes reads `games` through `results_known_at`.
- **Why not every public row.** `schedule` holds only games that went on to be played. A game postponed at short notice is missing, and a game delayed past its scheduled start is present. Until a game's result is public, its row's presence or absence would say how it turned out, as would a "plays tomorrow" input. So another game only shows once its result is public. A team's previous game, even the night before a back-to-back, is public by 10:00 UTC, before the 11:05 UTC slot.
- **Tests.** `tests/leakage/test_schedule.py` covers:
  - the locked column set
  - the exact 24-hour boundary
  - on game day, the schedule is known and the result is not
  - other games show only once their result is public, including the suspended Lake Tahoe game
  - the moved Lake Tahoe game's override
  - the schema's rejection of an earlier `observed_utc`
- **Final games only.** `schedule` covers the same final regular-season games as `games`, derived from the same parsed rows. Upcoming games for live predictions come from the schedule endpoint when phase 5 needs them.
- **The rule can be wrong** if a game was re-timed or re-dated less than 24 hours before it started. One case is known, and it has an override.
  - The Lake Tahoe game PHI at BOS (2020020290) was moved from 15:00 to 19:30 ET on 2021-02-21, after the sun delayed the previous day's game there. It is stored at its moved start.
  - When the move was announced is unverified, but it was public by the original 15:00 ET puck drop at the latest.
  - So its schedule counts as public from 20:00 UTC on 2021-02-21, not 24 h before the new start.

## Revisit when

A game turns up that was re-dated or re-timed within 24 hours of its start. Or a feature needs the schedule further ahead than the game being predicted, such as upcoming back-to-backs or road trips. That needs the schedule as published at the time, which this table does not hold.
