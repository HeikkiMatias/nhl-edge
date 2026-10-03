"""Decayed sums read point in time, shared by penalty rates (#104, ADR 0021) and finishing (#105,
ADR 0022).

A row's weight is 0.5 ** (d / half-life), d league game days back from the latest date read. A
running sum of each value times grow(day) serves every later as-of time: scaled back by the
latest day read (scale), it is the decayed sum then. Rows carry the league day (day) and when
they became public (observed_utc); targets carry as_of_utc, and read only rows public strictly
before it (before)."""

from collections.abc import Mapping, Sequence

import numpy as np
import polars as pl
from numpy.typing import NDArray


def later(a: pl.Expr, b: pl.Expr) -> pl.Expr:
    """The later of two times, either of which may be null. when/then rather than
    max_horizontal, which can fail to broadcast a column a one-row join left as a scalar."""
    return pl.when(b.is_null() | (a >= b)).then(a).otherwise(b)


def grow(day: pl.Expr, half_life: float) -> pl.Expr:
    """A row's weight times 2 ** (its day / half-life). Fifteen seasons of league days keep it
    under 2 ** 8 at a half-life of 360."""
    return pl.lit(2.0).pow(day / half_life)


def scale(half_life: float) -> pl.Expr:
    """What turns grown sums into decayed ones at the latest day read (reference)."""
    return pl.lit(2.0).pow(-pl.col("reference") / half_life)


def batches(rows: pl.DataFrame) -> pl.DataFrame:
    """Per publication time, the latest league day read up to then (reference)."""
    return (
        rows.group_by("observed_utc")
        .agg(pl.col("day").max())
        .sort("observed_utc")
        .select("observed_utc", reference=pl.col("day").cum_max(), known_utc="observed_utc")
    )


def player_history(rows: pl.DataFrame, values: Mapping[str, str], half_life: float) -> pl.DataFrame:
    """Each player's grown sums g_<name> of the values' columns ({name: column}) after each of
    his games, in publication order. rows are sorted by publication time."""
    weight = grow(pl.col("day"), half_life)
    by = pl.col("player_id")
    return rows.select(
        "player_id",
        "observed_utc",
        **{f"g_{n}": (pl.col(c) * weight).cum_sum().over(by) for n, c in values.items()},
    )


def role_history(rows: pl.DataFrame, values: Mapping[str, str], half_life: float) -> pl.DataFrame:
    """Each role's grown sums r_<name> over all its skaters after each publication time."""
    weight = grow(pl.col("day"), half_life)
    per_batch = (
        rows.group_by("role", "observed_utc")
        .agg(**{f"r_{n}": (pl.col(c) * weight).sum() for n, c in values.items()})
        .sort("observed_utc")
    )
    return per_batch.select(
        "role", "observed_utc", *(pl.col(f"r_{n}").cum_sum().over("role") for n in values)
    )


def before(
    left: pl.DataFrame, right: pl.DataFrame, by: str | Sequence[str] | None = None
) -> pl.DataFrame:
    """left with right's latest row public strictly before each as_of_utc."""
    return left.sort("as_of_utc").join_asof(
        right.sort("observed_utc"),
        left_on="as_of_utc",
        right_on="observed_utc",
        by=by,
        strategy="backward",
        allow_exact_matches=False,
        check_sortedness=False,
    )


def totals(rows: pl.DataFrame, group: Sequence[str], columns: Sequence[str]) -> pl.DataFrame:
    """Each group's sums of columns and its row count (games), as running sums in a fixed order
    rather than a group sum, which threads may add up in any order: a pull, a small difference
    of large sums, must not move in its last digits from one run to the next or with rows it
    never reads."""
    keys = list(group)
    return (
        rows.sort(*keys, "observed_utc", "game_id")
        .select(
            *keys,
            pl.col(*columns).cum_sum().over(keys),
            games=pl.int_range(1, pl.len() + 1).over(keys),
        )
        .group_by(keys, maintain_order=True)
        .last()
    )


def moments_pull(
    counts: NDArray[np.float64], exposure: NDArray[np.float64], noise: float = 1.0
) -> float:
    """The pull, in units of exposure, toward the average rate μ = Σ counts / Σ exposure, by the
    method of moments: v = (Σ exposure·(counts/exposure - μ)² - n·μ·noise) / Σ exposure is the
    spread of true rates once noise is taken out, and the pull is μ·noise / v. noise is a
    count's variance over its mean: 1 for Poisson counts, less for sums of probabilities. The
    pull is infinite, putting everyone at the average, when v is 0 or less."""
    mu = counts.sum() / exposure.sum()
    spread = (np.sum(exposure * (counts / exposure - mu) ** 2) - len(exposure) * mu * noise) / (
        exposure.sum()
    )
    return float(mu * noise / spread) if spread > 0 and mu > 0 else np.inf
