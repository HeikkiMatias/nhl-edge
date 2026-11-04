"""Point in time for scripts/targeted_features.py (#370, hard rule 1): density counts only games
on earlier dates, no column reads a later game, the goalie column reads only start probabilities
known before the prediction, and the script reads the lake only to the explore half."""

import sys
from datetime import date, timedelta
from typing import Any

import polars as pl
import pytest
from targeted_features_fixtures import load, schedule

from nhl_edge.backtest import yardstick as ys

tf = load()
D = date(2013, 11, 1)
BASE = [(1, D, "NYR", "BOS"), (2, D + timedelta(days=1), "PHI", "BOS")]


def test_density_never_counts_tonight_or_a_later_game() -> None:
    before = tf.density(schedule(BASE)).sort("game_id")
    later = schedule([*BASE, (3, D + timedelta(days=2), "BOS", "PHI")])
    after = tf.density(later).filter(pl.col("game_id") <= 2).sort("game_id")
    assert after.equals(before)
    # Another game on tonight's date is never counted as before it.
    same_day = schedule([*BASE, (4, D + timedelta(days=1), "NYR", "WSH")])
    row = tf.density(same_day).filter(pl.col("game_id") == 2).row(0, named=True)
    assert row == before.filter(pl.col("game_id") == 2).row(0, named=True)


def test_early_never_reads_a_later_game() -> None:
    games = schedule([(i, D + timedelta(days=2 * i), "NYR", "BOS") for i in range(1, 6)])
    frame = games.select("game_id", five_diff=pl.lit(0.3))
    more = schedule([(i, D + timedelta(days=2 * i), "NYR", "BOS") for i in range(1, 40)])
    early = tf.early(frame, games).sort("game_id")
    assert tf.early(frame, more).sort("game_id").equals(early)


def test_the_goalie_column_reads_only_candidates_known_before_the_prediction() -> None:
    # b2.known_before keeps a start probability only when observed before the prediction time.
    from datetime import UTC, datetime

    from nhl_edge.game import b2

    at = datetime(2013, 11, 1, 15, tzinfo=UTC)
    inputs = pl.DataFrame(
        {
            "game_id": [1],
            "home": ["NYR"],
            "away": ["BOS"],
            "observed_utc": [at - timedelta(hours=1)],
        }
    )
    pool = pl.DataFrame(
        {
            "game_id": [1, 1],
            "team": ["NYR", "NYR"],
            "goalie_id": [10, 11],
            "p_start": [0.9, 0.1],
            "observed_utc": [at - timedelta(hours=2), at + timedelta(minutes=1)],
        }
    )
    moments = pl.DataFrame({"game_id": [1], "prediction_utc": [at]})
    _, ready = b2.known_before(inputs, pool, moments, "observed_utc")
    assert ready["goalie_id"].to_list() == [10]


def test_the_script_reads_the_lake_only_to_the_explore_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[int] = []

    def read(lake: Any, last: int) -> None:
        asked.append(last)
        raise RuntimeError("stop here")

    monkeypatch.setattr(tf.ys, "read", read)
    monkeypatch.setattr(tf, "Lake", lambda: None)
    monkeypatch.setattr(sys, "argv", ["targeted_features.py"])
    with pytest.raises(RuntimeError, match="stop here"):
        tf.main()
    assert asked == [max(ys.EXPLORE)]


def test_the_family_and_its_rule_are_the_issues() -> None:
    assert tf.FAMILY == ("travel", "density", "early", "goalie_presumed", "lineup_change")
    assert tf.EARLY == 20 and tf.ALPHA == 0.05
    assert set(tf.PRIMARY) == set(tf.FAMILY) - {"lineup_change"}
