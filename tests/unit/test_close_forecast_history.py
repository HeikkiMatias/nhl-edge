"""scripts/close_forecast_history.py (#369, ADR 0054): the frozen fit, the measure against no
move, the size of the forecast moves, taking at the opener, and the simpler form, on rows built by
hand."""

import numpy as np
import polars as pl
import pytest
import yardstick_fixtures as yf
from close_forecast_fixtures import load, with_parts

from nhl_edge.backtest import yardstick as ys

cf = load()


def test_the_frozen_fit_recovers_the_weights() -> None:
    rng = np.random.default_rng(1)
    n = 4000
    rows = pl.DataFrame(
        {
            "o": rng.normal(0.2, 0.3, n),
            **{part: rng.normal(0, 0.1, n) for part in cf.PARTS},
        }
    )
    truth = {"a": 0.02, "b_o": -0.13, "five_diff": 0.3, "special_diff": 0.15}
    truth |= {"finishing_diff": 0.08, "goalie_diff": 0.08}
    y = truth["a"] + truth["b_o"] * rows["o"] + sum(truth[p] * rows[p] for p in cf.PARTS)
    rows = rows.with_columns(y=y + pl.Series(rng.normal(0, 0.001, n)))
    found = cf.frozen_fit(rows)
    assert list(found) == list(cf.WEIGHTS)
    for name, value in truth.items():
        assert found[name] == pytest.approx(value, abs=0.005)
    assert np.allclose(cf.forecast(rows, found), rows["y"].to_numpy(), atol=0.01)


def test_the_measure_is_against_no_move() -> None:
    games = pl.DataFrame({"y": [0.1, -0.2], "y_hat": [0.05, 0.0]})
    found = cf.against_no_move(games)["s"].to_list()
    assert found == pytest.approx([0.05**2 - 0.1**2, 0.2**2 - 0.2**2])


def test_the_forecast_moves_in_probability_points() -> None:
    # From an even opener, moves of 0, about 1.5 and about 2.5 points.
    found = cf.sizes(np.zeros(3), np.array([0.0, 0.06, 0.1]))
    assert found["at_1"] == pytest.approx(2 / 3) and found["at_2"] == pytest.approx(1 / 3)
    assert found["mean"] == pytest.approx((0.0 + 0.014995 + 0.024979) / 3, abs=1e-5)


def test_a_side_is_taken_on_its_expected_return_at_the_opener_price() -> None:
    games = pl.DataFrame(
        {
            "game_id": [1, 2],
            "season": [20152016, 20152016],
            "game_date": [None, None],
            "o": [0.0, 0.0],
            # Game 1's close is forecast at 0.55 for the home side; game 2's at the opener.
            "y_hat": [float(np.log(0.55 / 0.45)), 0.0],
            "p_close": [0.53, 0.5],
        }
    )
    prices = pl.DataFrame({"game_id": [1, 2], "home": [1.95, 1.95], "away": [1.95, 1.95]})
    taken = cf.takes(games, prices, 0.0)
    assert taken["game_id"].to_list() == [1]
    row = taken.row(0, named=True)
    assert row["ev"] == pytest.approx(0.55 * 1.95 - 1)
    assert row["clv"] == pytest.approx(0.53 * 1.95 - 1)
    assert row["moved"] == pytest.approx(0.03)
    # Above its expected return, nothing is taken.
    assert cf.takes(games, prices, 0.08).is_empty()


def test_the_simpler_form_is_scored_beside_the_split_on_the_same_games() -> None:
    frame = with_parts(yf.moves((*yf.SEASONS, 20152016)))
    found = cf.shrink(frame, (20142015, 20152016))
    assert sorted(found["season"].unique().to_list()) == [20142015, 20152016]
    assert found.select(pl.col("s_shrink", "s_split").is_null().any()).row(0) == (False, False)
    # A season needs an earlier one with an out-of-sample model gap.
    with pytest.raises(ValueError, match="no earlier season"):
        cf.shrink(frame, (ys.EXPLORE[0],))
