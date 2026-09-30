"""Backtest metrics with weekly block bootstrap intervals (hard rule 7).

Games in the same week share conditions (schedule density, injuries, a market's mood), so the
bootstrap resamples whole weeks, Monday to Sunday on the US Eastern game date, with replacement.
Weeks are drawn within each season, so a pooled interval keeps each season's weight. The interval
is the 2.5th to 97.5th percentile of the resampled means.

The same resampling gives an interval for a difference between two independent groups of games,
such as two eras of seasons, and for a statistic fitted on the games, such as B1's slope.
"""

from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass

import numpy as np
import polars as pl

DRAWS = 2000
SEED = 20260929
LEVEL = 0.95
# A probability is kept this far from 0 and 1, so one confident miss costs a finite log loss.
EPSILON = 1e-15


def log_loss(p: pl.Expr, y: pl.Expr) -> pl.Expr:
    """Per-game log loss of probability p for the binary outcome y (1 or 0)."""
    clipped = p.clip(EPSILON, 1 - EPSILON)
    return -(y * clipped.log() + (1 - y) * (1 - clipped).log())


@dataclass(frozen=True)
class Estimate:
    """A mean over games with its weekly block bootstrap interval."""

    mean: float
    low: float
    high: float
    games: int
    weeks: int

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


@dataclass(frozen=True)
class Interval:
    """A value with its weekly block bootstrap interval, for a difference or a fitted statistic."""

    value: float
    low: float
    high: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


def week_of(game_date: pl.Expr) -> pl.Expr:
    """The Monday that starts the game date's week."""
    return game_date.dt.truncate("1w")


def _weeks(frame: pl.DataFrame, *aggregates: pl.Expr) -> pl.DataFrame:
    """One row per season and week of the frame's games, in order: the games' row numbers (row),
    their count (games) and any further aggregates."""
    return (
        frame.with_row_index("row")
        .group_by("season", week_of(pl.col("game_date")).alias("week"))
        .agg(pl.col("row"), pl.len().alias("games"), *aggregates)
        .sort("season", "week")
    )


def _picks(
    weekly: pl.DataFrame, draws: int, rng: np.random.Generator
) -> Iterator[tuple[pl.DataFrame, np.ndarray]]:
    """Each season's weeks, with a draws by weeks array of that season's weeks drawn with
    replacement."""
    for (_,), season in weekly.group_by("season", maintain_order=True):
        yield season, rng.integers(0, season.height, size=(draws, season.height))


def interval(value: float, draws: np.ndarray) -> Interval:
    """value with the 2.5th to 97.5th percentile of its resampled draws."""
    tail = (1 - LEVEL) / 2 * 100
    low, high = np.percentile(draws, [tail, 100 - tail])
    return Interval(value=value, low=float(low), high=float(high))


def resampled_means(
    frame: pl.DataFrame, value: str, rng: np.random.Generator, draws: int = DRAWS
) -> np.ndarray:
    """The mean of value over the frame's games in each of draws resamples of its weeks."""
    if frame.is_empty():
        raise ValueError(f"no games to estimate {value} on")
    totals = np.zeros(draws)
    counts = np.zeros(draws)
    for season, picks in _picks(_weeks(frame, pl.col(value).sum().alias("total")), draws, rng):
        totals += season["total"].to_numpy()[picks].sum(axis=1)
        counts += season["games"].to_numpy()[picks].sum(axis=1)
    return totals / counts


def resampled_fits(
    frame: pl.DataFrame,
    statistic: Callable[[pl.DataFrame], dict[str, float]],
    rng: np.random.Generator,
    draws: int = DRAWS,
) -> dict[str, np.ndarray]:
    """Each named value of statistic, fitted on the frame's games in each of draws resamples of
    its weeks."""
    if frame.is_empty():
        raise ValueError("no games to fit on")
    seasons = [
        (season["row"].to_list(), picks) for season, picks in _picks(_weeks(frame), draws, rng)
    ]
    fitted: dict[str, np.ndarray] = {}
    for draw in range(draws):
        rows = np.concatenate([rows[week] for rows, picks in seasons for week in picks[draw]])
        for name, value in statistic(frame[rows]).items():
            fitted.setdefault(name, np.empty(draws))[draw] = value
    return fitted


def bootstrap(frame: pl.DataFrame, value: str, draws: int = DRAWS, seed: int = SEED) -> Estimate:
    """The mean of value over the frame's games, with a weekly block bootstrap interval. The frame
    needs season and game_date; weeks are resampled within each season."""
    means = resampled_means(frame, value, np.random.default_rng(seed), draws)
    spread = interval(float(frame[value].mean()), means)  # type: ignore[arg-type]
    return Estimate(
        mean=spread.value,
        low=spread.low,
        high=spread.high,
        games=frame.height,
        weeks=_weeks(frame).height,
    )


def difference(
    a: pl.DataFrame, b: pl.DataFrame, value: str, draws: int = DRAWS, seed: int = SEED
) -> Interval:
    """The mean of value over a's games minus its mean over b's, with a weekly block bootstrap
    interval. a and b are independent groups of games, such as two eras of seasons: each is
    resampled on its own."""
    rng = np.random.default_rng(seed)
    draws_a = resampled_means(a, value, rng, draws)
    draws_b = resampled_means(b, value, rng, draws)
    mean = float(a[value].mean()) - float(b[value].mean())  # type: ignore[arg-type]
    return interval(mean, draws_a - draws_b)
