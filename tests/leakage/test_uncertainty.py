"""u (#139, ADR 0026) reads only rows known before each game's prediction time: goalie starts,
projected lineups, replacements, earlier boxscores and career lines. A game's own lineup and
boxscore never move it, and its scale comes from training games only."""

from datetime import UTC, datetime, timedelta

import polars as pl
from polars.testing import assert_frame_equal
from uncertainty_fixtures import (
    BOX_SCHEMA,
    BOXSCORES,
    GAME,
    GOALIES,
    LINE_SCHEMA,
    LINES,
    LINEUPS,
    MOMENTS,
    PREDICTION,
    SEASON,
    SPARE,
    tables,
)

from nhl_edge.game import uncertainty as un

SKATERS = pl.DataFrame(
    {"game_id": [GAME] * 3, "season": [SEASON] * 3, "player_id": [101, 102, 103]}
).with_columns(prediction_utc=pl.lit(PREDICTION, dtype=pl.Datetime("us", "UTC")))


def earlier(**replaced: pl.DataFrame) -> dict[int, int]:
    counted = un.earlier_games(SKATERS, tables(**replaced))
    return dict(counted.select("player_id", "earlier_games").iter_rows())


def test_goalie_rows_known_only_at_the_prediction_time_are_not_read() -> None:
    late = GOALIES.with_columns(
        observed_utc=pl.when(pl.col("team") == "BOS")
        .then(pl.lit(PREDICTION))
        .otherwise(pl.col("observed_utc"))
    )
    surer = late.with_columns(
        p_start=pl.when(pl.col("team") == "BOS").then(0.99).otherwise(pl.col("p_start"))
    )
    assert_frame_equal(
        un.parts(tables(goalie_starts=late), MOMENTS),
        un.parts(tables(goalie_starts=surer), MOMENTS),
    )


def test_a_game_without_lineups_known_by_its_prediction_time_has_no_u() -> None:
    late = {"observed_utc": pl.lit(PREDICTION, dtype=pl.Datetime("us", "UTC"))}
    assert un.parts(
        tables(
            lineups=LINEUPS.with_columns(**late), lineup_replacements=SPARE.with_columns(**late)
        ),
        MOMENTS,
    ).is_empty()


def test_a_boxscore_public_at_or_after_the_prediction_time_does_not_count() -> None:
    own_and_late = pl.DataFrame(
        [
            # Public exactly at the prediction time, then the game's own boxscore the morning after.
            {
                "game_id": 2021020199,
                "season": SEASON,
                "team": "BOS",
                "player_id": 102,
                "role": "D",
                "observed_utc": PREDICTION,
            },
            {
                "game_id": GAME,
                "season": SEASON,
                "team": "BOS",
                "player_id": 102,
                "role": "D",
                "observed_utc": PREDICTION + timedelta(hours=19),
            },
            {
                "game_id": GAME,
                "season": SEASON,
                "team": "BOS",
                "player_id": 103,
                "role": "F",
                "observed_utc": PREDICTION + timedelta(hours=19),
            },
        ],
        schema=BOX_SCHEMA,
        orient="row",
    )
    assert (
        earlier()
        == earlier(actual_lineups=pl.concat([BOXSCORES, own_and_late]))
        == {
            101: 300,
            102: 70,
            103: 0,
        }
    )
    # Nor does it move u.
    assert_frame_equal(
        un.parts(tables(), MOMENTS),
        un.parts(tables(actual_lineups=pl.concat([BOXSCORES, own_and_late])), MOMENTS),
    )


def test_career_lines_count_only_for_earlier_seasons_once_public() -> None:
    later = pl.DataFrame(
        [
            # This season's own line, whatever its time, and a line public only after the game.
            {
                "player_id": 103,
                "season": SEASON,
                "league": "NHL",
                "game_type": 2,
                "games_played": 82,
                "observed_utc": datetime(2021, 10, 1, tzinfo=UTC),
            },
            {
                "player_id": 102,
                "season": 20192020,
                "league": "NHL",
                "game_type": 2,
                "games_played": 82,
                "observed_utc": PREDICTION,
            },
        ],
        schema=LINE_SCHEMA,
        orient="row",
    )
    assert earlier(player_league_seasons=pl.concat([LINES, later])) == earlier()


def test_the_scale_comes_from_the_training_games_alone() -> None:
    training = pl.DataFrame(
        {
            "goalie_doubt": [0.1, 0.3],
            "availability_doubt": [1.0, 3.0],
            "rookie_share": [0.1, 0.3],
            "observed_utc": [PREDICTION - timedelta(days=200)] * 2,
            "train_cutoff": [PREDICTION - timedelta(days=400), PREDICTION - timedelta(days=150)],
        }
    )
    scored = un.parts(tables(), MOMENTS)
    other = scored.with_columns(rookie_share=pl.lit(0.9))
    scale = un.fit_scale(training)
    # Each scored game's u depends on its own parts and the training scale, never on another
    # scored game.
    both = scale.score(pl.concat([scored, other]))
    assert both[0] == scale.score(scored)[0]
    # The scale's cutoff is the latest of its rows' times and of the fitted tables they read, so a
    # fold can refuse a scale fitted on rows it could not have had.
    assert scale.train_cutoff == PREDICTION - timedelta(days=150)


def test_parts_carry_the_latest_cutoff_of_the_fitted_tables_they_read() -> None:
    later = PREDICTION - timedelta(hours=1)
    refit = LINEUPS.with_columns(
        train_cutoff=pl.when(pl.col("team") == "TOR")
        .then(pl.lit(later))
        .otherwise(pl.col("train_cutoff"))
    )
    assert un.parts(tables(lineups=refit), MOMENTS)["train_cutoff"][0] == later
