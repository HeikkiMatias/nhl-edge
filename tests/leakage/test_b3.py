"""Point-in-time rules for B3 (#106, ADR 0023, hard rules 1 and 9). A game's prediction reads its
projected lineups, ratings, league rates, power plays, multipliers and schedule terms only once
known, mixes over the goalie-start probabilities, and never reads its own result, lineup or
starters. Each fold's fit reads only games whose results, boxscores and rows were public before
the fold starts, and a fold before the tuning cutoff is refused."""

from datetime import timedelta

import numpy as np
import polars as pl
import pytest
from b3_fixtures import league
from polars.testing import assert_frame_equal

from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.game import b3

LEAGUE = league()
TEST = 20182019
START = fold_start(LEAGUE.games.select("season", "start_utc"), TEST)
MOMENTS = LEAGUE.games.filter(pl.col("season") == TEST).select(
    "game_id", prediction_utc="start_utc"
)
# The per-game tables a prediction reads, each by game_id.
PER_GAME = (
    "schedule_terms",
    "lineups",
    "lineup_replacements",
    "player_ratings",
    "expected_power_plays",
    "goal_multipliers",
)
UTC_TYPE = pl.Datetime("us", "UTC")


def replaced(**frames: pl.DataFrame) -> b3.Tables:
    return b3.Tables(**{**LEAGUE.__dict__, **frames})


def predict(tables: b3.Tables = LEAGUE) -> pl.DataFrame:
    predicted, _ = b3.predictions(tables, MOMENTS, TEST, START)
    return predicted.sort("game_id")


BEFORE = predict()


def same(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    try:
        assert_frame_equal(left, right, rel_tol=1e-12, abs_tol=1e-12)
    except AssertionError:
        return False
    return True


def retimed(table: pl.DataFrame, which: pl.Expr, moment: object) -> pl.DataFrame:
    return table.with_columns(
        observed_utc=pl.when(which).then(pl.lit(moment, UTC_TYPE)).otherwise(pl.col("observed_utc"))
    )


def test_a_games_own_result_lineup_and_starters_never_move_its_prediction() -> None:
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
    # Without its own boxscore at all, the same.
    unplayed = LEAGUE.actual_lineups.filter(~tested)
    assert same(predict(replaced(actual_lineups=unplayed)), BEFORE)


def test_earlier_results_do_move_the_fit() -> None:
    # The guard above is not vacuous.
    earlier = pl.col("season") < TEST
    games = LEAGUE.games.with_columns(
        home_score=pl.when(earlier).then(pl.col("away_score")).otherwise(pl.col("home_score")),
        away_score=pl.when(earlier).then(pl.col("home_score")).otherwise(pl.col("away_score")),
    )
    assert not same(predict(replaced(games=games)), BEFORE)


@pytest.mark.parametrize("name", PER_GAME)
def test_a_row_known_only_at_the_prediction_time_is_not_read(name: str) -> None:
    game = MOMENTS["game_id"][0]
    moment = MOMENTS.filter(pl.col("game_id") == game)["prediction_utc"].item()
    table = getattr(LEAGUE, name)
    late = retimed(table, pl.col("game_id") == game, moment)
    predicted = predict(replaced(**{name: late}))
    assert game not in predicted["game_id"].to_list()
    assert same(predicted, BEFORE.filter(pl.col("game_id") != game))


def test_one_skaters_rating_known_only_at_the_prediction_time_is_not_read() -> None:
    game = MOMENTS["game_id"][0]
    moment = MOMENTS.filter(pl.col("game_id") == game)["prediction_utc"].item()
    player = LEAGUE.player_ratings.filter(pl.col("game_id") == game)["player_id"][0]
    which = (pl.col("game_id") == game) & (pl.col("player_id") == player)
    predicted = predict(replaced(player_ratings=retimed(LEAGUE.player_ratings, which, moment)))
    assert game not in predicted["game_id"].to_list()


def test_league_rates_known_only_at_the_prediction_time_are_not_read() -> None:
    game = MOMENTS["game_id"][0]
    day = LEAGUE.games.filter(pl.col("game_id") == game)["game_date"].item()
    moment = MOMENTS.filter(pl.col("game_id") == game)["prediction_utc"].item()
    late = retimed(LEAGUE.rapm_terms, pl.col("game_date") == day, moment)
    on_day = LEAGUE.games.filter(pl.col("game_date") == day)["game_id"]
    predicted = predict(replaced(rapm_terms=late))
    assert not predicted["game_id"].is_in(on_day.implode()).any()
    assert same(predicted, BEFORE.filter(~pl.col("game_id").is_in(on_day.implode())))


def test_a_goalie_start_row_known_only_at_the_prediction_time_is_not_read() -> None:
    game = MOMENTS["game_id"][0]
    moment = MOMENTS.filter(pl.col("game_id") == game)["prediction_utc"].item()
    late = retimed(LEAGUE.goalie_starts, pl.col("game_id") == game, moment)
    dropped = LEAGUE.goalie_starts.filter(pl.col("game_id") != game)
    assert same(predict(replaced(goalie_starts=late)), predict(replaced(goalie_starts=dropped)))
    # Without candidates, both goalies count as average: the prediction moves.
    assert not same(predict(replaced(goalie_starts=dropped)), BEFORE)


def coefficients(tables: b3.Tables) -> np.ndarray:
    _, model = b3.predictions(tables, MOMENTS, TEST, START)
    return np.array([model.intercept, *model.weights])


LAST = LEAGUE.games.filter(pl.col("season") == TEST - 10001)["game_id"].max()
IS_LAST = pl.col("game_id") == LAST


def test_an_earlier_result_published_after_the_fold_start_is_not_fitted_on() -> None:
    published_late = LEAGUE.games.with_columns(
        observed_utc=pl.when(IS_LAST)
        .then(pl.lit(START + timedelta(hours=1)))
        .otherwise(pl.col("observed_utc")),
        home_score=pl.when(IS_LAST).then(pl.lit(9, pl.Int16)).otherwise(pl.col("home_score")),
    )
    np.testing.assert_allclose(
        coefficients(replaced(games=published_late)),
        coefficients(replaced(games=LEAGUE.games.filter(~IS_LAST))),
        atol=1e-9,
    )


def test_an_earlier_boxscore_published_after_the_fold_start_is_not_fitted_on() -> None:
    lineups = retimed(LEAGUE.actual_lineups, IS_LAST, START + timedelta(hours=1))
    np.testing.assert_allclose(
        coefficients(replaced(actual_lineups=lineups)),
        coefficients(replaced(games=LEAGUE.games.filter(~IS_LAST))),
        atol=1e-9,
    )


@pytest.mark.parametrize("name", PER_GAME)
def test_an_earlier_row_published_after_the_fold_start_leaves_its_game_out(name: str) -> None:
    late = retimed(getattr(LEAGUE, name), IS_LAST, START + timedelta(days=30))
    np.testing.assert_allclose(
        coefficients(replaced(**{name: late})),
        coefficients(replaced(games=LEAGUE.games.filter(~IS_LAST))),
        atol=1e-9,
    )


def test_a_fold_before_a_tuning_cutoff_is_refused() -> None:
    assert (BEFORE["train_cutoff"] < START).all()
    assert (BEFORE["train_cutoff"] >= b3.TUNED_CUTOFF).all()
    # A table retuned on results after the fold start moves the cutoff past it.
    for name in ("schedule_terms", "player_ratings", "expected_power_plays", "goal_multipliers"):
        table = getattr(LEAGUE, name)
        retuned = table.with_columns(train_cutoff=pl.lit(START + timedelta(days=1), UTC_TYPE))
        with pytest.raises(ValueError, match="before the tuning cutoff"):
            b3.predictions(replaced(**{name: retuned}), MOMENTS, TEST, START)
    early = league((20152016, 20162017), games=40)
    start = fold_start(early.games.select("season", "start_utc"), 20162017)
    moments = early.games.filter(pl.col("season") == 20162017).select(
        "game_id", prediction_utc="start_utc"
    )
    with pytest.raises(ValueError, match="before the tuning cutoff"):
        b3.predictions(early, moments, 20162017, start)


def test_the_fits_cutoff_is_the_last_row_it_read() -> None:
    # An earlier game's boxscore public a day before the fold start, after the tuning cutoff: the
    # fit reads it, so its train_cutoff is that moment, not the last result.
    moment = START - timedelta(days=1)
    lineups = retimed(LEAGUE.actual_lineups, IS_LAST, moment)
    _, model = b3.predictions(replaced(actual_lineups=lineups), MOMENTS, TEST, START)
    assert model.train_cutoff == moment > b3.TUNED_CUTOFF
    # Likewise for a projection row.
    projection = retimed(LEAGUE.lineups, IS_LAST, moment)
    _, model = b3.predictions(replaced(lineups=projection), MOMENTS, TEST, START)
    assert model.train_cutoff == moment
