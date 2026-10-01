"""Point-in-time rules for stints (#97, ADR 0015). A game's stints come only from its own feeds,
public at 10:00 UTC the morning after (ADR 0004), and from its season's xG model, fitted before
the season began. So no prediction for the game sees them, and RAPM (#101) may read a game's
stints only once they are public. The column set is locked here, as for the per-game tables."""

from datetime import timedelta

import pandera.errors
import polars as pl
import pytest
from feed_fixtures import MTL_ARI, OPENING_WEEK_GAMES, assert_public_the_morning_after, parsed_feeds
from stint_fixtures import (
    OBSERVED,
    XG_VERSION,
    coverage_frame,
    faceoffs_frame,
    full_period,
    lineups_frame,
    shifts_frame,
    shot_xg_frame,
    shots_frame,
)

from nhl_edge.features import stints
from nhl_edge.lake.schemas import Stints, dtypes

GAMES = (*OPENING_WEEK_GAMES, MTL_ARI)

COLUMNS = [
    "game_id",
    "season",
    "game_date",
    "stint_id",
    "period",
    "start_s",
    "end_s",
    "seconds",
    "home_skaters",
    "away_skaters",
    "home_goalie",
    "away_goalie",
    "strength",
    "score_state",
    "zone_start",
    "home_xg",
    "away_xg",
    "home_goals",
    "away_goals",
    "drop_reason",
    "xg_version",
    "xg_train_cutoff",
    "observed_utc",
]


def stints_of(game_ids: tuple[int, ...]) -> pl.DataFrame:
    tables = [parsed_feeds(game_id) for game_id in game_ids]
    joined = {name: pl.concat([t[name] for t in tables]) for name in tables[0]}
    shot_xg = pl.DataFrame(
        schema={
            "game_id": pl.Int64,
            "event_id": pl.Int32,
            "xg": pl.Float64,
            "train_cutoff": pl.Datetime("us", "UTC"),
            "artifact_version": pl.String,
        }
    )
    return stints.build(
        joined["shift_coverage"],
        joined["shifts"],
        joined["actual_lineups"],
        joined["shots"],
        shot_xg,
        joined["faceoffs"],
    )


@pytest.mark.parametrize("game_id", GAMES)
def test_a_game_never_sees_its_own_stints(game_id: int) -> None:
    assert_public_the_morning_after(stints_of((game_id,)), game_id)


@pytest.mark.parametrize("game_id", GAMES)
def test_a_game_never_sees_its_own_players_seconds(game_id: int) -> None:
    assert_public_the_morning_after(stints.player_seconds(stints_of((game_id,))), game_id)


def test_a_games_stints_read_nothing_of_any_other_game() -> None:
    alone = stints_of((OPENING_WEEK_GAMES[0],))
    together = stints_of(GAMES).filter(pl.col("game_id") == OPENING_WEEK_GAMES[0])
    assert alone.equals(together)


def test_xg_from_a_model_fitted_after_the_game_is_refused() -> None:
    late = shot_xg_frame({1: 0.1}).with_columns(train_cutoff=pl.lit(OBSERVED + timedelta(days=1)))
    with pytest.raises(pandera.errors.SchemaError):
        stints.build(
            coverage_frame(),
            shifts_frame(full_period()),
            lineups_frame(),
            shots_frame([(1, 100, True, False)]),
            late,
            faceoffs_frame([]),
        )
    fitted_before = shot_xg_frame({1: 0.1})
    frame = stints.build(
        coverage_frame(),
        shifts_frame(full_period()),
        lineups_frame(),
        shots_frame([(1, 100, True, False)]),
        fitted_before,
        faceoffs_frame([]),
    )
    assert frame["xg_version"].to_list() == [XG_VERSION]


def test_stints_hold_only_the_games_own_play_and_its_xg_model() -> None:
    assert list(dtypes(Stints)) == COLUMNS
