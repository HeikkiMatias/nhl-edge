from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from nhl_edge.backtest.metrics import (
    SEED,
    bootstrap,
    difference,
    holm,
    p_value,
    resampled_means,
    slope,
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


def test_a_slope_refits_on_each_resample_of_the_weeks() -> None:
    # y = 0.4 x + noise: the slope comes back with an interval around it (#251).
    rng = np.random.default_rng(3)
    lines = frame().with_columns(x=pl.Series(rng.normal(0, 0.3, 200)))
    lines = lines.with_columns(y=0.4 * pl.col("x") + pl.Series(rng.normal(0, 0.05, 200)))
    fit = slope(lines, "x", "y")
    x, y = lines["x"].to_numpy(), lines["y"].to_numpy()
    assert fit.value == pytest.approx(np.polyfit(x, y, 1)[0])
    assert fit.low < 0.4 < fit.high and fit.high - fit.low < 0.1
    # An exact line gives the same slope on every resample.
    exact = lines.with_columns(y=2 * pl.col("x") + 1)
    assert (slope(exact, "x", "y").low, slope(exact, "x", "y").high) == pytest.approx((2, 2))
    with pytest.raises(ValueError, match="too few games"):
        slope(lines.head(1), "x", "y")
    # A constant x has no slope: refused, not divided by zero (Codex on #261).
    with pytest.raises(ValueError, match="no spread"):
        slope(lines.with_columns(x=pl.lit(0.0)), "x", "y")


def test_the_p_value_comes_from_the_intervals_own_resamples() -> None:
    # ADR 0048: twice the smaller tail of the same resampled means the interval reads.
    centred = frame().with_columns(value=pl.col("value") - 0.62)
    means = resampled_means(centred, "value", np.random.default_rng(SEED), 2000)
    expected = 2 * min(np.mean(means <= 0), np.mean(means >= 0))
    assert p_value(centred, "value") == pytest.approx(expected)
    assert 0 < p_value(centred, "value") <= 1
    # An interval excluding 0 has a p-value under 0.05; a mean far from 0, about 0.
    estimate = bootstrap(frame(), "value")
    assert estimate.low > 0 and p_value(frame(), "value") < 0.05
    assert p_value(frame(), "value") == 0.0
    # Both tails at once can't exceed 1.
    zero = frame().with_columns(value=pl.lit(0.0))
    assert p_value(zero, "value") == 1.0


def test_holms_step_down_tightens_with_the_familys_size() -> None:
    # Four tests at family-wise 5%: thresholds 0.0125, 0.0167, 0.025 and 0.05 by rank.
    family = {"a": 0.01, "b": 0.04, "c": 0.016, "d": 0.03}
    result = holm(family)
    assert [result[n]["rank"] for n in "acdb"] == [1, 2, 3, 4]
    assert result["a"]["threshold"] == pytest.approx(0.05 / 4)
    assert result["c"]["threshold"] == pytest.approx(0.05 / 3)
    assert [result[n]["passes"] for n in "acdb"] == [True, True, False, False]
    # b's own p-value is below its threshold, 0.05, but the step-down stops at d.
    assert result["b"]["p_value"] <= result["b"]["threshold"]
    # One more test in the family, and c no longer passes: its threshold falls to 0.0125.
    larger = holm(family | {"e": 0.9})
    assert larger["a"]["passes"] and not larger["c"]["passes"]
    # Alone, a test passes at alpha itself; a tie ranks by name.
    assert holm({"x": 0.05})["x"]["passes"]
    assert holm({"y": 0.01, "x": 0.01})["x"]["rank"] == 1
    assert holm({}) == {}
