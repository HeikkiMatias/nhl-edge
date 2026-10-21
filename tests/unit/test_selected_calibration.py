"""The blend's calibration on the bets the policy selects (#253): each game on its side, the three
splits fixed on #253, and their figures against B1."""

from datetime import date, timedelta

import numpy as np
import polars as pl

from nhl_edge.backtest import selected
from nhl_edge.betting import selection

DAY = date(2021, 10, 18)  # a Monday


def scored(n: int, weeks: int = 1, seed: int = 7) -> pl.DataFrame:
    """n games over the given weeks: the blend's home probability, B1's, the two prices, u, the
    result and the policy's pick, as selection.select leaves them."""
    rng = np.random.default_rng(seed)
    p = rng.uniform(0.3, 0.7, n)
    frame = pl.DataFrame(
        {
            "season": [20212022] * n,
            "game_id": list(range(1, n + 1)),
            "game_date": [DAY + timedelta(days=7 * (i % weeks)) for i in range(n)],
            "p_home": p,
            "p_b1_home": np.clip(p + rng.normal(0, 0.03, n), 0.05, 0.95),
            "home_price": 1 / np.clip(p - 0.02, 0.05, 0.95),
            "away_price": 1 / np.clip(1 - p - 0.02, 0.05, 0.95),
            "u_sd": rng.normal(0, 1, n),
            "home_win": (rng.uniform(0, 1, n) < p).astype(np.int8),
        }
    )
    return selection.select(frame)


def test_each_game_is_scored_on_its_side_of_higher_expected_return() -> None:
    frame = pl.DataFrame(
        {
            "season": [20212022] * 3,
            "game_date": [DAY] * 3,
            "side": ["home", "away", "away"],
            "price": [1.8, 2.4, 2.0],
            "p_side": [0.6, 0.45, 0.5],
            "home_price": [1.8, 1.6, 2.0],
            "away_price": [2.1, 2.4, 2.0],
            "p_b1_home": [0.55, 0.62, 0.5],
            "u_sd": [-0.5, 0.5, None],
            "home_win": [1, 1, 0],
            "picked": [True, None, False],
        },
        schema_overrides={"home_win": pl.Int8},
    )
    sides = selected.sides(frame, "p_b1_home", "picked")
    assert sides["won"].to_list() == [1, 0, 1]
    assert sides["p_b1"].to_list() == [0.55, 1 - 0.62, 0.5]
    # the side's price below the other's is the favourite; a tie counts as an underdog
    assert sides["favourite"].to_list() == [True, False, False]
    assert sides["u_low"].to_list() == [True, False, None]
    assert sides["selected"].to_list() == [True, False, False]


def test_a_group_below_enough_gives_its_counts_only() -> None:
    frame = selected.sides(scored(40, weeks=2), "p_b1_home", "picked")
    figures = selected.figures(frame, lambda f: selected.weeks(f) >= 4)
    assert figures == {"games": 40, "weeks": 2}


def test_a_group_with_enough_gets_its_figures_against_b1() -> None:
    frame = selected.sides(scored(600, weeks=8), "p_b1_home", "picked")
    figures = selected.figures(frame, lambda f: selected.weeks(f) >= 4)
    assert figures["games"] == 600 and figures["weeks"] == 8
    for name in ("log_loss_minus_b1", "brier_minus_b1"):
        spread = figures[name]
        assert spread["low"] <= spread["mean"] <= spread["high"] and spread["games"] == 600
    assert figures["slope"]["low"] < figures["slope"]["value"] < figures["slope"]["high"]
    assert "intercept" in figures


def test_the_blend_better_than_b1_reads_negative() -> None:
    # The blend knows the result better than B1 on every game.
    frame = scored(400, weeks=6)
    sharp = frame.with_columns(
        p_side=pl.when(pl.col("side") == "home")
        .then(pl.col("home_win") * 0.6 + 0.2)
        .otherwise(0.8 - pl.col("home_win") * 0.6)
    )
    figures = selected.figures(selected.sides(sharp, "p_b1_home", "picked"), lambda f: True)
    assert figures["log_loss_minus_b1"]["high"] < 0 and figures["brier_minus_b1"]["high"] < 0


def test_the_splits_give_both_groups_and_count_games_without_u() -> None:
    frame = scored(300, weeks=6).with_columns(
        u_sd=pl.when(pl.col("game_id") <= 10).then(None).otherwise(pl.col("u_sd"))
    )
    splits = selected.splits(selected.sides(frame, "p_b1_home", "picked"), lambda f: True)
    assert splits["games"] == 300
    bets, other = splits["selected"]["bets"], splits["selected"]["other games"]
    assert bets["games"] + other["games"] == 300 and splits["selected"]["left_out"] == 0
    price = splits["price"]
    assert price["favourites"]["games"] + price["underdogs"]["games"] == 300
    u = splits["u"]
    assert u["left_out"] == 10
    assert u["u below its training mean"]["games"] + u["u at or above it"]["games"] == 290


def test_a_group_of_one_outcome_keeps_its_other_figures() -> None:
    frame = selected.sides(scored(60, weeks=5), "p_b1_home", "picked").with_columns(
        won=pl.lit(1, pl.Int8)
    )
    figures = selected.figures(frame, lambda f: True)
    assert "log_loss_minus_b1" in figures and "slope" not in figures
