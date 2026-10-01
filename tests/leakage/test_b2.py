"""Point-in-time rules for B2 (#78, ADR 0013, hard rules 1 and 9). A game's prediction reads its
own feature rows only once known, mixes over the goalie-start probabilities, and never reads its
own result or starters. Each fold's fit reads only games whose results, boxscores and features
were public before the fold starts, and a fold before the tuning cutoff is refused."""

from datetime import timedelta

import numpy as np
import polars as pl
from b2_fixtures import league
from polars.testing import assert_frame_equal

from nhl_edge.backtest.seasons import SeasonRole, season_role
from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.game import b2

LEAGUE = league()
TEST = 20182019
START = fold_start(LEAGUE.games.select("season", "start_utc"), TEST)
MOMENTS = LEAGUE.games.filter(pl.col("season") == TEST).select(
    "game_id", prediction_utc="start_utc"
)


def replaced(**frames: pl.DataFrame) -> b2.Tables:
    return b2.Tables(**{**LEAGUE.__dict__, **frames})


def predict(tables: b2.Tables = LEAGUE) -> pl.DataFrame:
    predicted, _ = b2.predictions(tables, MOMENTS, TEST, START, b2.TUNED)
    return predicted.sort("game_id")


BEFORE = predict()


def same(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    try:
        assert_frame_equal(left, right, rel_tol=1e-12, abs_tol=1e-12)
    except AssertionError:
        return False
    return True


def test_a_games_own_result_and_starters_never_move_its_prediction() -> None:
    tested = pl.col("game_id").is_in(MOMENTS["game_id"].implode())
    games = LEAGUE.games.with_columns(
        home_score=pl.when(tested).then(pl.col("away_score")).otherwise(pl.col("home_score")),
        away_score=pl.when(tested).then(pl.col("home_score")).otherwise(pl.col("away_score")),
    )
    lineups = LEAGUE.actual_lineups.with_columns(
        starting_goalie=pl.when(tested)
        .then(~pl.col("starting_goalie"))
        .otherwise(pl.col("starting_goalie"))
    )
    assert same(predict(replaced(games=games, actual_lineups=lineups)), BEFORE)


def test_earlier_results_do_move_the_fit() -> None:
    # The guard above is not vacuous.
    earlier = pl.col("season") < TEST
    games = LEAGUE.games.with_columns(
        home_score=pl.when(earlier).then(pl.col("away_score")).otherwise(pl.col("home_score")),
        away_score=pl.when(earlier).then(pl.col("home_score")).otherwise(pl.col("away_score")),
    )
    assert not same(predict(replaced(games=games)), BEFORE)


def test_a_feature_row_known_only_at_the_prediction_time_is_not_read() -> None:
    game = MOMENTS["game_id"][0]
    moment = MOMENTS.filter(pl.col("game_id") == game)["prediction_utc"].item()
    late = LEAGUE.team_strength.with_columns(
        observed_utc=pl.when(pl.col("game_id") == game)
        .then(pl.lit(moment))
        .otherwise(pl.col("observed_utc"))
    )
    predicted = predict(replaced(team_strength=late))
    assert game not in predicted["game_id"].to_list()
    assert same(predicted, BEFORE.filter(pl.col("game_id") != game))


def test_a_candidate_known_only_at_the_prediction_time_is_not_read() -> None:
    # Without its goalie rows, a team's goalie counts as average: the prediction moves.
    game = MOMENTS["game_id"][0]
    moment = MOMENTS.filter(pl.col("game_id") == game)["prediction_utc"].item()
    late = LEAGUE.goalie_effects.with_columns(
        observed_utc=pl.when(pl.col("game_id") == game)
        .then(pl.lit(moment))
        .otherwise(pl.col("observed_utc"))
    )
    dropped = predict(
        replaced(goalie_effects=LEAGUE.goalie_effects.filter(pl.col("game_id") != game))
    )
    assert same(predict(replaced(goalie_effects=late)), dropped)
    assert not same(dropped, BEFORE)


def coefficients(tables: b2.Tables) -> np.ndarray:
    _, model = b2.predictions(tables, MOMENTS, TEST, START, b2.TUNED)
    return np.array([model.intercept, *model.weights])


def test_an_earlier_result_published_after_the_fold_start_is_not_fitted_on() -> None:
    last = LEAGUE.games.filter(pl.col("season") == TEST - 10001)["game_id"].max()
    late = pl.col("game_id") == last
    published_late = LEAGUE.games.with_columns(
        observed_utc=pl.when(late)
        .then(pl.lit(START + timedelta(hours=1)))
        .otherwise(pl.col("observed_utc")),
        home_score=pl.when(late).then(pl.lit(9, pl.Int16)).otherwise(pl.col("home_score")),
    )
    left_out = LEAGUE.games.filter(~late)
    np.testing.assert_allclose(
        coefficients(replaced(games=published_late)),
        coefficients(replaced(games=left_out)),
        atol=1e-9,
    )


def test_an_earlier_boxscore_published_after_the_fold_start_is_not_fitted_on() -> None:
    last = LEAGUE.games.filter(pl.col("season") == TEST - 10001)["game_id"].max()
    late = pl.col("game_id") == last
    lineups = LEAGUE.actual_lineups.with_columns(
        observed_utc=pl.when(late)
        .then(pl.lit(START + timedelta(hours=1)))
        .otherwise(pl.col("observed_utc"))
    )
    left_out = replaced(games=LEAGUE.games.filter(~late))
    np.testing.assert_allclose(
        coefficients(replaced(actual_lineups=lineups)), coefficients(left_out), atol=1e-9
    )


def test_every_fit_predates_its_fold_and_a_fold_before_the_tuning_cutoff_is_refused() -> None:
    assert (BEFORE["train_cutoff"] < START).all()
    assert (MOMENTS["prediction_utc"] >= START).all()
    assert START > b2.TUNED_CUTOFF


def test_tuning_reads_the_training_seasons_only() -> None:
    assert all(season_role(s) is SeasonRole.TRAINING for s in b2.TUNING_SEASONS)
    assert max(b2.TUNING_SEASONS) == 20172018
