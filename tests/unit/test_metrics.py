from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from nhl_edge.backtest.metrics import (
    SEED,
    bootstrap,
    difference,
    resampled_means,
)


def frame(seed: int = 7) -> pl.DataFrame:
    """200 games over two seasons, with values around 0.65."""
    rng = np.random.default_rng(seed)
    days = [date(2018, 10, 3) + timedelta(days=int(d)) for d in rng.integers(0, 60, 120)]
    days += [date(2021, 10, 12) + timedelta(days=int(d)) for d in rng.integers(0, 60, 80)]
    return pl.DataFrame(
        {
            "season": [20182019] * 120 + [20212022] * 80,
            "game_date": days,
            "value": rng.normal(0.65, 0.2, 200),
        }
    )


def test_the_bootstrap_gives_the_figures_it_gave_before_its_resampling_was_shared() -> None:
    # Taken from the bootstrap before #65 split out its resampling: every earlier run's
    # intervals stay as they were.
    estimate = bootstrap(frame(), "value")
    assert (estimate.mean, estimate.low, estimate.high) == pytest.approx(
        (0.6256441837147406, 0.5971054934672712, 0.6521880466230153), rel=1e-12
    )
    assert (estimate.games, estimate.weeks) == (200, 18)


def test_a_difference_between_groups_resamples_each_on_its_own() -> None:
    a = frame().with_columns(value=pl.lit(1.0))
    b = frame(seed=8).with_columns(value=pl.lit(0.25))
    # Every resample of a constant is that constant, so the interval is the difference itself.
    spread = difference(a, b, "value")
    assert (spread.value, spread.low, spread.high) == pytest.approx((0.75, 0.75, 0.75))
    varied = difference(frame(), frame(seed=8), "value")
    assert varied.low < varied.value < varied.high
    means = [float(games["value"].mean()) for games in (frame(), frame(seed=8))]  # type: ignore[arg-type]
    assert varied.value == pytest.approx(means[0] - means[1])


def test_resampling_needs_games() -> None:
    empty = frame().clear()
    with pytest.raises(ValueError, match="no games"):
        resampled_means(empty, "value", np.random.default_rng(SEED))
