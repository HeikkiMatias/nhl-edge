"""Backtest metrics with weekly block bootstrap intervals (hard rule 7).

Games in the same week share conditions (schedule density, injuries, a market's mood), so the
bootstrap resamples whole weeks, Monday to Sunday on the US Eastern game date, with replacement.
Weeks are drawn within each season, so a pooled interval keeps each season's weight. The interval
is the 2.5th to 97.5th percentile of the resampled means.
"""

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


def week_of(game_date: pl.Expr) -> pl.Expr:
    """The Monday that starts the game date's week."""
    return game_date.dt.truncate("1w")


def bootstrap(frame: pl.DataFrame, value: str, draws: int = DRAWS, seed: int = SEED) -> Estimate:
    """The mean of value over the frame's games, with a weekly block bootstrap interval. The frame
    needs season and game_date; weeks are resampled within each season."""
    if frame.is_empty():
        raise ValueError(f"no games to estimate {value} on")
    weekly = (
        frame.group_by("season", week_of(pl.col("game_date")).alias("week"))
        .agg(pl.col(value).sum().alias("total"), pl.len().alias("games"))
        .sort("season", "week")
    )
    rng = np.random.default_rng(seed)
    totals = np.zeros(draws)
    counts = np.zeros(draws)
    for (_,), season in weekly.group_by("season", maintain_order=True):
        total = season["total"].to_numpy()
        games = season["games"].to_numpy()
        picks = rng.integers(0, season.height, size=(draws, season.height))
        totals += total[picks].sum(axis=1)
        counts += games[picks].sum(axis=1)
    tail = (1 - LEVEL) / 2 * 100
    low, high = np.percentile(totals / counts, [tail, 100 - tail])
    return Estimate(
        mean=float(frame[value].mean()),  # type: ignore[arg-type]
        low=float(low),
        high=float(high),
        games=frame.height,
        weeks=weekly.height,
    )
