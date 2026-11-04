"""Point in time for scripts/targeted_features.py (#370, hard rule 1): density counts only games
on earlier dates, no column reads a later game, the goalie column reads only start probabilities
known before the prediction, and the script reads the lake only to the explore half."""

import sys
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

import polars as pl
import pytest
import yardstick_fixtures as yf
from targeted_features_fixtures import load, moments, schedule

from nhl_edge.backtest import yardstick as ys

tf = load()
D = date(2013, 11, 1)
BASE = [(1, D, "NYR", "BOS"), (2, D + timedelta(days=1), "PHI", "BOS")]


def density(games: pl.DataFrame) -> pl.DataFrame:
    return tf.density(games, moments(games)).sort("game_id")


def test_density_never_counts_tonight_or_a_later_game() -> None:
    before = density(schedule(BASE))
    later = schedule([*BASE, (3, D + timedelta(days=2), "BOS", "PHI")])
    assert density(later).filter(pl.col("game_id") <= 2).equals(before)
    # Another game on tonight's date is never counted as before it.
    same_day = schedule([*BASE, (4, D + timedelta(days=1), "NYR", "WSH")])
    row = density(same_day).filter(pl.col("game_id") == 2).row(0, named=True)
    assert row == before.filter(pl.col("game_id") == 2).row(0, named=True)


def test_a_result_not_yet_public_at_the_prediction_is_not_counted() -> None:
    # Game 1's result turns public only after game 2's prediction: game 2 counts nothing before.
    late = schedule(BASE).with_columns(
        observed_utc=pl.when(pl.col("game_id") == 1)
        .then(pl.col("observed_utc") + timedelta(hours=6))
        .otherwise(pl.col("observed_utc"))
    )
    row = density(late).filter(pl.col("game_id") == 2).row(0, named=True)
    assert row["dense3_diff"] == 0 and row["mismatch"] == 0 and row["away_trip"] == 0
    on_time = density(schedule(BASE)).filter(pl.col("game_id") == 2).row(0, named=True)
    assert on_time["dense3_diff"] == -1 and on_time["mismatch"] == 1 and on_time["away_trip"] == 1


def test_early_never_reads_a_later_game() -> None:
    games = schedule([(i, D + timedelta(days=2 * i), "NYR", "BOS") for i in range(1, 6)])
    frame = moments(games).with_columns(five_diff=pl.lit(0.3))
    more = schedule([(i, D + timedelta(days=2 * i), "NYR", "BOS") for i in range(1, 40)])
    early = tf.early(frame, games).sort("game_id")
    assert tf.early(frame, more).sort("game_id").equals(early)


def test_travel_and_the_goalie_column_are_as_known_at_the_prediction() -> None:
    teams, players, sbr = yf.league()
    teams, players = ys.through(teams, 20142015), ys.through(players, 20142015)
    market = ys.market(sbr, players.games)
    now = market.select("game_id", "prediction_utc")
    assert tf.travel(teams, now).height == market.height
    pairs = tf.goalie_pairs(players, now)
    assert pairs["game_id"].n_unique() == market.height
    # A moment at the inputs' own as-of time, before the opener's, finds none known yet.
    early = now.with_columns(pl.col("prediction_utc") - timedelta(seconds=1))
    assert tf.travel(teams, early).is_empty() and tf.goalie_pairs(players, early).is_empty()
    # The starters who played are never read: without their boxscores, nothing changes.
    blind = replace(players, actual_lineups=players.actual_lineups.clear())
    assert tf.presumed(tf.goalie_pairs(blind, now)).equals(tf.presumed(pairs))
    # A start probability observed after the prediction is never read: with every candidate
    # late, each team gets one average goalie and the presumed starter is the mixture.
    late = players.goalie_starts.with_columns(
        observed_utc=pl.col("observed_utc") + timedelta(days=400)
    )
    unknown = tf.presumed(tf.goalie_pairs(replace(players, goalie_starts=late), now))
    assert unknown["goalie_presumed"].abs().max() == 0


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
