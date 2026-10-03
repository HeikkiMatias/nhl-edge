"""The gap screen (#107, hard rule 8) never reads a game's result: the reviewer checks inputs, the
projection and who dressed and started, never who won."""

import polars as pl
from b3_fixtures import league
from polars.testing import assert_frame_equal

from nhl_edge.audit import b3_gaps
from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.game import b3

LEAGUE = league()
TEST = 20182019
START = fold_start(LEAGUE.games.select("season", "start_utc"), TEST)


def test_the_gap_games_results_never_move_the_screen() -> None:
    games = LEAGUE.games.filter(pl.col("season") == TEST).sort("game_id").head(20)
    moments = games.select("game_id", prediction_utc="start_utc")
    predicted, _ = b3.predictions(LEAGUE, moments, TEST, START)
    gaps = (
        games.select("season", "game_id", "game_date", "home", "away")
        .join(predicted.select("game_id", p_b3="p_home"), on="game_id")
        .with_columns(p_b1=pl.col("p_b3") - 0.1, gap=pl.lit(0.1))
    )
    before = b3_gaps.screen(LEAGUE, gaps, {TEST: START})
    tested = pl.col("game_id").is_in(games["game_id"].implode())
    flipped = LEAGUE.games.with_columns(
        home_score=pl.when(tested).then(pl.col("away_score")).otherwise(pl.col("home_score")),
        away_score=pl.when(tested).then(pl.col("home_score")).otherwise(pl.col("away_score")),
    )
    after = b3_gaps.screen(b3.Tables(**{**LEAGUE.__dict__, "games": flipped}), gaps, {TEST: START})
    assert_frame_equal(before, after)


def test_a_result_in_the_gaps_file_never_reaches_the_screen() -> None:
    games = LEAGUE.games.filter(pl.col("season") == TEST).sort("game_id").head(5)
    moments = games.select("game_id", prediction_utc="start_utc")
    predicted, _ = b3.predictions(LEAGUE, moments, TEST, START)
    gaps = (
        games.select("season", "game_id", "game_date", "home", "away", "home_score")
        .join(predicted.select("game_id", p_b3="p_home"), on="game_id")
        .with_columns(p_b1=pl.col("p_b3") - 0.1, gap=pl.lit(0.1), home_win=pl.lit(1))
    )
    screened = b3_gaps.screen(LEAGUE, gaps, {TEST: START})
    assert not {"home_win", "home_score"} & set(screened.columns)
