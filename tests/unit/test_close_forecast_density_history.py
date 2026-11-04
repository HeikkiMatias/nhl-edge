"""scripts/close_forecast_density_history.py (ADR 0056): version 2's frozen fit, both versions out
of sample on the same games, the stop on the rested games, and the artifact, on rows built by
hand."""

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import polars as pl
import pytest
import yardstick_fixtures as yf
from close_forecast_fixtures import load_density, with_density

from nhl_edge.backtest import yardstick as ys
from nhl_edge.features.density import DENSITY
from nhl_edge.game import b3

h2 = load_density()
SEASONS = (*yf.SEASONS, 20152016)


def test_the_frozen_fit_has_densitys_weights_beside_the_split() -> None:
    frame = with_density(yf.moves(SEASONS, games=400)).filter(pl.col("season") >= ys.FIRST_FIT)
    found = h2.frozen_fit(frame)
    assert list(found) == ["a", "b_o", *b3.SPLIT, *DENSITY]
    # The move follows mismatch by 0.08 and the 5v5 part by 0.5; the rest is noise.
    assert found["mismatch"] == pytest.approx(0.08, abs=0.01)
    assert found["five_diff"] == pytest.approx(0.5, abs=0.05)
    assert abs(found["away_trip"]) < 0.01


def test_both_versions_are_scored_out_of_sample_on_the_same_games() -> None:
    frame = with_density(yf.moves(SEASONS, games=400))
    seasons = (20132014, 20142015, 20152016)
    games = h2.out_of_sample(frame, seasons)
    assert sorted(games["season"].unique().to_list()) == list(seasons)
    assert games.height == frame.filter(pl.col("season").is_in(seasons)).height
    assert np.allclose(games["difference"], games["s_2"] - games["s_1"])
    # Version 1 is version 1's script's own split regression.
    alone = h2.against_no_move(ys.scored(frame, b3.SPLIT, seasons)).sort("game_id")
    assert np.allclose(games["s_1"], alone["s"])
    # Density moves the close here, so version 2 beats version 1.
    assert games["difference"].mean() < 0  # type: ignore[operator]


def test_the_subsets_split_the_games_by_rest() -> None:
    games = pl.DataFrame({"rested": [True, False, True]})
    assert h2.subset(games, "every").height == 3
    assert h2.subset(games, "rested").height == 2
    assert h2.subset(games, "back_to_back").height == 1


def test_the_build_stops_unless_the_rested_games_favour_version_2() -> None:
    assert not h2.stop({"mean": -0.4, "low": -0.5, "high": -0.3})
    # A point estimate below 0 is not enough (hard rule 7).
    assert h2.stop({"mean": -0.4, "low": -0.9, "high": 0.1})
    assert h2.stop({"mean": 0.2, "low": 0.1, "high": 0.3})
    assert h2.stop({"mean": -0.4, "low": -0.5, "high": 0.0})


def test_the_artifact_carries_both_parts_and_their_train_cutoff(tmp_path: Path) -> None:
    frame = with_density(yf.moves(SEASONS)).filter(pl.col("season") >= ys.FIRST_FIT)
    weights = h2.frozen_fit(frame)
    path = h2.write_artifact(tmp_path, weights, frame, "close-forecast-2-20261104-abc1234")
    found = json.loads(path.read_text())
    assert found["component"] == "close-forecast-2" and found["adr"] == "0056"
    assert found["weights"] == pytest.approx(weights)
    assert found["inputs"] == ["1", "o", *b3.SPLIT, *DENSITY]
    assert found["split"] == ["a", "b_o", *b3.SPLIT] and found["density"] == list(DENSITY)
    cutoff = frame["result_utc"].max()
    assert isinstance(cutoff, datetime)
    assert found["train_cutoff"] == cutoff.isoformat()
