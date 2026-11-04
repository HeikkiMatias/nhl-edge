"""Point in time for scripts/lineup_change.py (#370, hard rules 1 and 9): the change reads only
earlier boxscores public before the prediction, tonight's projection observed before it, and
ratings observed before it; never tonight's own boxscore or a later game."""

import sys
from datetime import timedelta
from typing import Any

import polars as pl
import pytest
from lineup_change_fixtures import (
    PREDICTION,
    TONIGHT,
    D,
    boxscores,
    feature,
    load,
    projected,
    public,
    rated,
)

from nhl_edge.backtest import yardstick as ys

lc = load()
BASE = feature()


def test_tonights_own_boxscore_and_a_later_game_are_never_read() -> None:
    tonight = boxscores([(TONIGHT, D + timedelta(days=2), "BOS", 999, "F", 5000)])
    later = boxscores([(4, D + timedelta(days=3), "BOS", 998, "F", 5000)])
    every = pl.concat([boxscores(), tonight, later])
    assert feature(actual_lineups=every) == pytest.approx(BASE)


def test_a_boxscore_public_only_after_the_prediction_is_not_the_last_game() -> None:
    late = boxscores().with_columns(
        observed_utc=pl.when(pl.col("game_id") == 1)
        .then(pl.lit(PREDICTION + timedelta(seconds=1)))
        .otherwise(pl.col("observed_utc"))
    )
    # BOS then has no earlier game it knew of: its change is 0, and NYR's is 0.
    assert feature(actual_lineups=late) == 0.0


def test_a_rating_observed_after_the_prediction_is_never_read() -> None:
    later = pl.concat([rated(), rated([(4, 105, 5.0, PREDICTION + timedelta(seconds=1))])])
    assert feature(player_ratings=later) == pytest.approx(BASE)
    # Observed before it, the same rating counts: the check has power.
    sooner = pl.concat([rated(), rated([(2, 105, 5.0, PREDICTION - timedelta(minutes=5))])])
    assert feature(player_ratings=sooner) != pytest.approx(BASE)


def test_a_projection_observed_after_the_prediction_is_never_read() -> None:
    late = pl.concat(
        [
            projected(),
            projected([("BOS", 999, "F", 50.0)]).with_columns(
                observed_utc=pl.lit(PREDICTION).cast(pl.Datetime("us", "UTC"))
            ),
        ]
    )
    assert feature(lineups=late) == pytest.approx(BASE)


def test_the_last_game_is_the_latest_known_whatever_came_after() -> None:
    # A second BOS game, public before the prediction, replaces game 1 as the last game.
    second = boxscores([(5, D + timedelta(days=1), "BOS", 101, "F", 1000)])
    assert public(D + timedelta(days=1)) < PREDICTION
    assert feature(actual_lineups=pl.concat([boxscores(), second])) != pytest.approx(BASE)


def test_the_script_reads_the_lake_only_to_the_explore_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[int] = []

    def read(lake: Any, last: int) -> None:
        asked.append(last)
        raise RuntimeError("stop here")

    monkeypatch.setattr(lc.ys, "read", read)
    monkeypatch.setattr(lc, "Lake", lambda: None)
    monkeypatch.setattr(sys, "argv", ["lineup_change.py"])
    with pytest.raises(RuntimeError, match="stop here"):
        lc.main()
    assert asked == [max(ys.EXPLORE)]
