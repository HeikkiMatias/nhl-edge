"""A slate's games as targets of the feature builders (#162).

Every feature table lists final games only. To rate games not played yet, a builder appends the
slate's games to the frames it rates games from, after its input checks, so the checks never flag
a target's missing boxscore:
- with_targets adds them to games with their schedule columns and null results (scores,
  decided_in and observed_utc), so no builder can read a result for them;
- with_schedule_targets adds them to schedule as schedule rows, for schedule_terms.

A builder rates each game only from history public before its as-of time, the earlier of 10:00 ET
and an hour before the start, so a target's row is the quantity history's rows are
(tests/leakage/test_live_targets.py). Only a slate fetched before that time gives targets
(in_time): a late response never counts as an on-time input (#170). A slate game already in games
is final: its history row is that row, so it is not added again.
"""

import polars as pl

from nhl_edge.features.team_strength import as_of
from nhl_edge.lake.schemas import SCHEDULE_LEAD

SCHEDULE_COLUMNS = (
    "game_id",
    "season",
    "game_date",
    "start_utc",
    "home",
    "away",
    "venue",
    "neutral_site",
    "limited_attendance",
    "raw_key",
)


def in_time(slate: pl.DataFrame) -> pl.DataFrame:
    """The slate's games fetched before their as-of time. A game fetched at or after it gets no
    target row: schedule terms would rate it at the fetch time, after results and rest that its
    history row never sees."""
    return slate.filter(pl.col("observed_utc") < as_of(pl.col("game_date"), pl.col("start_utc")))


def unplayed(frame: pl.DataFrame, slate: pl.DataFrame) -> pl.DataFrame:
    """The slate's games that frame does not hold."""
    return slate.join(frame.select("game_id"), on="game_id", how="anti")


def with_targets(games: pl.DataFrame, slate: pl.DataFrame) -> pl.DataFrame:
    """games with the slate's unplayed games appended, their results null."""
    added = unplayed(games, slate).select(SCHEDULE_COLUMNS)
    return pl.concat([games, added], how="diagonal").sort("game_id")


def with_schedule_targets(schedule: pl.DataFrame, slate: pl.DataFrame) -> pl.DataFrame:
    """schedule with the slate's unplayed games appended as schedule rows, each public at the
    later of its fetch and a day before its start: never before history's rows would be
    (SCHEDULE_LEAD, ADR 0005), nor before this installation saw it."""
    added = unplayed(schedule, slate).with_columns(
        observed_utc=pl.max_horizontal("observed_utc", pl.col("start_utc") - SCHEDULE_LEAD)
    )
    return pl.concat([schedule, added.select(schedule.columns)]).sort("game_id")


def rated(frame: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """A feature table's rows of the games being rated, on the dates they are rated. A target row
    left by a night whose games were postponed is no game's row, even once its game is played on
    another date."""
    keys = ["game_id", "game_date"] if "game_date" in frame.columns else ["game_id"]
    return frame.join(games.select(keys), on=keys, how="semi")
