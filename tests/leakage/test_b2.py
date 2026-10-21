"""Point-in-time rules for B2 (#78, ADR 0013, hard rules 1 and 9). A game's prediction reads its
own feature rows only once known, mixes over the goalie-start probabilities, and never reads its
own result or starters. Each fold's fit reads only games whose results, boxscores and features
were public before the fold starts, and a fold before the tuning cutoff is refused."""

from datetime import UTC, datetime, time, timedelta

import numpy as np
import polars as pl
import pytest
from b2_fixtures import league
from polars.testing import assert_frame_equal

from nhl_edge.backtest.market import PREDICTION_LAG
from nhl_edge.backtest.seasons import SeasonRole, season_role
from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.game import b2
from nhl_edge.ingest.sbr import open_assumed_utc
from nhl_edge.lake.schemas import SCHEDULE_LEAD

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


def test_an_earlier_goalie_row_published_after_the_fold_start_is_not_fitted_on() -> None:
    # The starter's row unknown at the fold start counts as an average goalie, whatever it says.
    last = LEAGUE.games.filter(pl.col("season") == TEST - 10001)["game_id"].max()
    late = pl.col("game_id") == last
    published_late = LEAGUE.goalie_effects.with_columns(
        observed_utc=pl.when(late)
        .then(pl.lit(START + timedelta(days=30)))
        .otherwise(pl.col("observed_utc")),
        goals_saved=pl.when(late).then(pl.lit(5.0)).otherwise(pl.col("goals_saved")),
    )
    missing = LEAGUE.goalie_effects.filter(~late)
    np.testing.assert_allclose(
        coefficients(replaced(goalie_effects=published_late)),
        coefficients(replaced(goalie_effects=missing)),
        atol=1e-9,
    )


def test_an_earlier_feature_row_published_after_the_fold_start_leaves_its_game_out() -> None:
    last = LEAGUE.games.filter(pl.col("season") == TEST - 10001)["game_id"].max()
    late = pl.col("game_id") == last
    for name in ("team_strength", "schedule_terms"):
        table = getattr(LEAGUE, name)
        published_late = table.with_columns(
            observed_utc=pl.when(late)
            .then(pl.lit(START + timedelta(days=30)))
            .otherwise(pl.col("observed_utc"))
        )
        np.testing.assert_allclose(
            coefficients(replaced(**{name: published_late})),
            coefficients(replaced(games=LEAGUE.games.filter(~late))),
            atol=1e-9,
        )


def test_at_e2s_time_a_game_retimed_on_the_day_waits_for_its_schedule() -> None:
    # E2 predicts a second after 10:00 ET. A game whose schedule terms became known only at
    # 20:00 UTC that day is not predicted then, but is at its start (E1).
    game = MOMENTS["game_id"][0]
    day = LEAGUE.games.filter(pl.col("game_id") == game)["game_date"].item()
    starts = LEAGUE.games.filter(pl.col("season") == TEST).select(
        "game_id", "game_date", "start_utc"
    )
    e2 = pl.DataFrame(
        {
            "game_id": starts["game_id"],
            "prediction_utc": [
                open_assumed_utc(
                    row["game_date"], row["start_utc"], row["start_utc"] - SCHEDULE_LEAD
                )
                + PREDICTION_LAG
                for row in starts.iter_rows(named=True)
            ],
        }
    ).with_columns(pl.col("prediction_utc").cast(pl.Datetime("us", "UTC")))
    retimed = LEAGUE.schedule_terms.with_columns(
        observed_utc=pl.when(pl.col("game_id") == game)
        .then(pl.lit(datetime.combine(day, time(20), UTC)))
        .otherwise(pl.col("observed_utc"))
    )
    at_e2, _ = b2.predictions(replaced(schedule_terms=retimed), e2, TEST, START, b2.TUNED)
    at_e1, _ = b2.predictions(replaced(schedule_terms=retimed), MOMENTS, TEST, START, b2.TUNED)
    assert game not in at_e2["game_id"].to_list() and at_e2.height == MOMENTS.height - 1
    assert game in at_e1["game_id"].to_list()


def test_a_goalie_start_row_known_only_at_the_prediction_time_is_not_read() -> None:
    game = MOMENTS["game_id"][0]
    moment = MOMENTS.filter(pl.col("game_id") == game)["prediction_utc"].item()
    late = LEAGUE.goalie_starts.with_columns(
        observed_utc=pl.when(pl.col("game_id") == game)
        .then(pl.lit(moment))
        .otherwise(pl.col("observed_utc"))
    )
    dropped = LEAGUE.goalie_starts.filter(pl.col("game_id") != game)
    assert same(predict(replaced(goalie_starts=late)), predict(replaced(goalie_starts=dropped)))


def test_a_fold_before_a_tuning_cutoff_is_refused() -> None:
    assert (BEFORE["train_cutoff"] < START).all()
    assert (BEFORE["train_cutoff"] >= b2.TUNED_CUTOFF).all()
    # A feature table whose own-season rows were retuned on results after the fold start moves
    # the cutoff past it: goalie_starts too (#132), which B2 reads at prediction time.
    assert "goalie_starts" in b2.CUTOFF_TABLES
    utc = pl.Datetime("us", "UTC")
    for name in b2.CUTOFF_TABLES:
        table = getattr(LEAGUE, name)
        if "season" not in table.columns:  # the fixture's team strength has none; the lake's has
            table = table.join(LEAGUE.games.select("game_id", "season"), on="game_id")
        retuned = table.with_columns(
            train_cutoff=pl.when(pl.col("season") == TEST)
            .then(pl.lit(START + timedelta(days=1), utc))
            .otherwise(pl.lit(b2.TUNED_CUTOFF, utc))
        )
        with pytest.raises(ValueError, match="before the tuning cutoff"):
            b2.predictions(replaced(**{name: retuned}), MOMENTS, TEST, START, b2.TUNED)
    early = league((20152016, 20162017), games=40)
    start = fold_start(early.games.select("season", "start_utc"), 20162017)
    moments = early.games.filter(pl.col("season") == 20162017).select(
        "game_id", prediction_utc="start_utc"
    )
    with pytest.raises(ValueError, match="before the tuning cutoff"):
        b2.predictions(early, moments, 20162017, start, b2.TUNED)


def test_a_later_seasons_cutoffs_do_not_refuse_the_fold() -> None:
    # #132: goalie_starts is refit each season, so its later seasons' rows carry later cutoffs
    # that this fold never reads.
    utc = pl.Datetime("us", "UTC")
    later = START + timedelta(days=200)
    copied = LEAGUE.goalie_starts.filter(pl.col("season") == TEST).with_columns(
        pl.col("game_id") + 1_000_000_000,
        season=pl.lit(TEST + 10001, pl.Int32),
        train_cutoff=pl.lit(later, utc),
        observed_utc=pl.lit(later, utc),
    )
    tables = replaced(goalie_starts=pl.concat([LEAGUE.goalie_starts, copied]))
    assert b2.tuning_cutoff(tables, TEST) == b2.tuning_cutoff(LEAGUE, TEST)
    assert b2.tuning_cutoff(tables, TEST + 10001) == later
    assert same(predict(tables), BEFORE)


def test_the_fits_cutoff_is_the_last_row_it_read() -> None:
    # An earlier game's boxscore public a day before the fold start, after the tuning cutoff: the
    # fit reads it, so its train_cutoff is that moment, not the last result.
    last = LEAGUE.games.filter(pl.col("season") == TEST - 10001)["game_id"].max()
    moment = START - timedelta(days=1)
    lineups = LEAGUE.actual_lineups.with_columns(
        observed_utc=pl.when(pl.col("game_id") == last)
        .then(pl.lit(moment))
        .otherwise(pl.col("observed_utc"))
    )
    _, model = b2.predictions(replaced(actual_lineups=lineups), MOMENTS, TEST, START, b2.TUNED)
    assert model.train_cutoff == moment > b2.TUNED_CUTOFF


def test_tuning_reads_the_training_seasons_only() -> None:
    assert all(season_role(s) is SeasonRole.TRAINING for s in b2.TUNING_SEASONS)
    assert max(b2.TUNING_SEASONS) == 20172018
