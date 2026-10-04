"""The market blend (#140, ADR 0027) learns only from out-of-sample predictions of the folds
before each tested season (hard rule 6), from rows known before the fold starts, and its
predictions never read the tested season's results."""

from datetime import timedelta

import polars as pl
import pytest
from blend_fixtures import rows, starts

from nhl_edge.backtest import blend as blend_backtest

SEASONS = [20182019, 20192020, 20202021, 20212022]
TESTED = [20182019, 20212022]


def blended(every: pl.DataFrame) -> pl.DataFrame:
    predictions, _, _, _ = blend_backtest.run(every, starts(every), TESTED)
    return predictions.select("experiment", "model", "game_id", "p_home").sort(
        "experiment", "model", "game_id"
    )


def test_the_tested_seasons_results_never_move_its_blend() -> None:
    every = rows(SEASONS)
    flipped = every.with_columns(
        home_win=pl.when(pl.col("season") == 20212022)
        .then(1 - pl.col("home_win"))
        .otherwise(pl.col("home_win"))
    )
    assert blended(every).equals(blended(flipped))


def test_a_later_season_never_moves_an_earlier_blend() -> None:
    every = rows(SEASONS)
    later = rows([20222023])
    assert blended(every).equals(blended(pl.concat([every, later])))


def test_earlier_folds_results_do_move_it() -> None:
    every = rows(SEASONS)
    flipped = every.with_columns(
        home_win=pl.when(pl.col("season") == 20192020)
        .then(1 - pl.col("home_win"))
        .otherwise(pl.col("home_win"))
    )
    assert not blended(every).equals(blended(flipped))


def test_a_result_public_only_after_the_fold_starts_is_not_fitted_on() -> None:
    every = rows(SEASONS)
    start = starts(every)[("E1", 20212022)]
    late_id = every.filter(season=20202021)["game_id"][-1]
    late = every.with_columns(
        result_utc=pl.when(pl.col("game_id") == late_id)
        .then(pl.lit(start + timedelta(days=1)))
        .otherwise(pl.col("result_utc"))
    )
    _, fits, _, coverage = blend_backtest.run(late, starts(late), TESTED)
    assert coverage["E1"][20212022]["blend_trained_on"] == 1799
    assert fits["E1"][20212022]["BLEND"].train_cutoff < start


@pytest.mark.parametrize("column", ["p_b3_cutoff", "p_b2_cutoff", "parts_utc", "parts_cutoff"])
def test_a_training_input_known_only_after_the_fold_starts_refuses_the_fold(column: str) -> None:
    every = rows(SEASONS)
    start = starts(every)[("E1", 20212022)]
    leaked = every.with_columns(
        pl.when(pl.col("season") == 20202021)
        .then(pl.lit(start))
        .otherwise(pl.col(column))
        .alias(column)
    )
    with pytest.raises(ValueError, match="after its fold starts"):
        blend_backtest.run(leaked, starts(leaked), TESTED)


def test_every_blend_prediction_is_cut_off_before_its_fold() -> None:
    every = rows(SEASONS, experiments=("E1", "E2"))
    fold = starts(every)
    predictions, fits, scales, _ = blend_backtest.run(every, fold, TESTED)
    for (experiment, season), frame in predictions.group_by("experiment", "season"):
        start = fold[(str(experiment), int(season))]  # type: ignore[arg-type]
        assert (frame["train_cutoff"] < start).all()
        assert scales[str(experiment)][int(season)].train_cutoff < start  # type: ignore[arg-type]
        for model in fits[str(experiment)][int(season)].values():  # type: ignore[arg-type]
            assert model.train_cutoff < start
