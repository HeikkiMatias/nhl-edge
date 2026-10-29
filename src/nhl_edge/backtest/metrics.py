"""Backtest metrics with weekly block bootstrap intervals (hard rule 7).

Games in the same week share conditions (schedule density, injuries, a market's mood), so the
bootstrap resamples whole weeks, Monday to Sunday on the US Eastern game date, with replacement.
Weeks are drawn within each season, so a pooled interval keeps each season's weight. The interval
is the 2.5th to 97.5th percentile of the resampled means.

The same resampling gives an interval for a difference between two independent groups of games,
such as two eras of seasons, and a two-sided p-value for a mean against 0, which Holm's step-down
adjusts over a family of tests (ADR 0048).
"""

from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from typing import Any

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
    """A value with its weekly block bootstrap interval, such as a difference between groups."""

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


def p_value(frame: pl.DataFrame, value: str, draws: int = DRAWS, seed: int = SEED) -> float:
    """The two-sided p-value of value's mean against 0 from the same weekly block bootstrap that
    gives its interval (bootstrap, ADR 0048): twice the smaller of the shares of the resampled
    means at or below 0 and at or above 0, at most 1."""
    means = resampled_means(frame, value, np.random.default_rng(seed), draws)
    return float(min(1.0, 2 * min(np.mean(means <= 0), np.mean(means >= 0))))


def holm(p_values: Mapping[str, float], alpha: float = 0.05) -> dict[str, dict[str, Any]]:
    """Holm's step-down over the family p_values at family-wise alpha (ADR 0048): sorted from the
    smallest, the k-th passes if it and every one before it is at most alpha / (m - k + 1). It
    holds the chance of any false pass to alpha under any dependence between the tests. Each
    name's p-value, rank, threshold and whether it passes; ties are ranked by name."""
    m = len(p_values)
    result: dict[str, dict[str, Any]] = {}
    passing = True
    ordered = sorted(p_values.items(), key=lambda item: (item[1], item[0]))
    for rank, (name, p) in enumerate(ordered, start=1):
        threshold = alpha / (m - rank + 1)
        passing = passing and p <= threshold
        result[name] = {"p_value": p, "rank": rank, "threshold": threshold, "passes": passing}
    return result


def slope(frame: pl.DataFrame, x: str, y: str, draws: int = DRAWS, seed: int = SEED) -> Interval:
    """The least-squares slope of y on x, with an intercept, over the frame's games, with a
    weekly block bootstrap interval: each draw refits the slope on the resampled weeks' sums. The
    frame needs season and game_date."""
    if frame.height < 2:
        raise ValueError(f"too few games to estimate the slope of {y} on {x}")
    sums = {
        "sx": pl.col(x).sum(),
        "sy": pl.col(y).sum(),
        "sxy": (pl.col(x) * pl.col(y)).sum(),
        "sxx": (pl.col(x) * pl.col(x)).sum(),
    }
    totals = {name: np.zeros(draws) for name in (*sums, "n")}
    weekly = _weeks(frame, *(expr.alias(name) for name, expr in sums.items()))
    for season, picks in _picks(weekly, draws, np.random.default_rng(seed)):
        for name in sums:
            totals[name] += season[name].to_numpy()[picks].sum(axis=1)
        totals["n"] += season["games"].to_numpy()[picks].sum(axis=1)

    def fit(n: Any, sx: Any, sy: Any, sxy: Any, sxx: Any) -> Any:
        return (n * sxy - sx * sy) / (n * sxx - sx * sx)

    whole = {k: float(v) for k, v in frame.select(**sums, n=pl.len()).row(0, named=True).items()}
    if whole["n"] * whole["sxx"] - whole["sx"] ** 2 <= 0:
        raise ValueError(f"{x} has no spread to estimate the slope of {y} on")
    with np.errstate(divide="ignore", invalid="ignore"):
        resampled = fit(**totals)
    # A resample whose weeks all share one value of x has no slope, and is left out.
    finite = resampled[np.isfinite(resampled)]
    if finite.size == 0:
        raise ValueError(f"no resample of {x} has the spread to estimate the slope of {y} on")
    return interval(fit(**whole), finite)


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


def _calibration_fit(logit: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Intercept and slope of a logistic regression of y on logit, by Newton's method."""
    x = np.column_stack([np.ones_like(logit), logit])
    beta = np.array([0.0, 1.0])
    for _ in range(50):
        p = 1 / (1 + np.exp(-(x @ beta)))
        hessian = (x * (p * (1 - p))[:, None]).T @ x
        step = np.linalg.solve(hessian + 1e-12 * np.eye(2), x.T @ (y - p))
        beta = beta + step
        if np.abs(step).max() < 1e-10:
            break
    return float(beta[0]), float(beta[1])


def calibration_draws(
    frame: pl.DataFrame, p: str, y: str, draws: int = DRAWS, seed: int = SEED
) -> tuple[tuple[float, float], np.ndarray]:
    """The calibration intercept and slope of probability p for outcome y, from a logistic
    regression of y on p's log-odds, and their refits on draws weekly block bootstrap resamples
    (a draws by 2 array). The frame needs season and game_date."""
    if frame.is_empty():
        raise ValueError(f"no games to estimate {p}'s calibration on")
    clipped = frame[p].clip(EPSILON, 1 - EPSILON).to_numpy()
    logit = np.log(clipped / (1 - clipped))
    outcome = frame[y].cast(pl.Float64).to_numpy()
    fit = _calibration_fit(logit, outcome)
    rng = np.random.default_rng(seed)
    seasons = [
        (season["row"].to_list(), picks) for season, picks in _picks(_weeks(frame), draws, rng)
    ]
    fitted = np.empty((draws, 2))
    for d in range(draws):
        rows = np.concatenate(
            [np.concatenate([weeks[w] for w in picks[d]]) for weeks, picks in seasons]
        ).astype(int)
        fitted[d] = _calibration_fit(logit[rows], outcome[rows])
    return fit, fitted


def calibration(
    frame: pl.DataFrame, p: str, y: str, draws: int = DRAWS, seed: int = SEED
) -> dict[str, Interval]:
    """The calibration intercept and slope of probability p for outcome y, from a logistic
    regression of y on p's log-odds, with weekly block bootstrap intervals: 0 and 1 when
    calibrated. The frame needs season and game_date."""
    (intercept, slope), fitted = calibration_draws(frame, p, y, draws, seed)
    return {
        "intercept": interval(intercept, fitted[:, 0]),
        "slope": interval(slope, fitted[:, 1]),
    }
