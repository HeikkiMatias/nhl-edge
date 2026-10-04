"""Bet selection on expected return at the executable price (docs/plan.md §5, §11, hard rule 4,
#141, ADR 0028).

For each game, the expected return of each side at its own decimal price o:

    EV_home = p·o_home - 1        EV_away = (1 - p)·o_away - 1

where p is the blend's full-game home probability (hard rule 2). The side with the higher EV is
picked when its EV clears the hurdle: MIN_EV, plus EV_PER_SD for each standard deviation of the
uncertainty score u above its training mean (ADR 0026). At most one bet per game; with a margin,
both sides can't clear it. The probability gap is never read (hard rule 4): a 52.5% blend against
a 50% fair price at 1.90 has an EV of -0.25% and is no bet.
"""

from dataclasses import dataclass

import polars as pl

HOME = "home"
AWAY = "away"


@dataclass(frozen=True)
class Policy:
    """The selection and staking policy (ADR 0028), frozen before live games."""

    # The lowest expected return a bet needs at the executable price.
    min_ev: float = 0.025
    # The hurdle rises this much per standard deviation of u above its training mean.
    ev_per_sd: float = 0.01
    # The share of the Kelly stake bet.
    kelly: float = 0.25
    # A bet's cap, and a day's, as shares of the bankroll at the start of the day.
    max_bet: float = 0.015
    max_day: float = 0.05
    # Each season's paper bankroll, in units.
    bankroll: float = 100.0


POLICY = Policy()


def doubt(u_sd: pl.Expr) -> pl.Expr:
    """u's standard deviations above its training mean, or 0 at or below it."""
    return pl.when(u_sd > 0).then(u_sd).otherwise(0.0)


def select(games: pl.DataFrame, policy: Policy = POLICY) -> pl.DataFrame:
    """Each game (game_id, p_home, home_price, away_price, u_sd and any other columns) with the
    side of higher expected return (side), its price, probability and EV, the game's hurdle, and
    whether it is bet (picked)."""
    p = pl.col("p_home")
    ev_home = p * pl.col("home_price") - 1
    ev_away = (1 - p) * pl.col("away_price") - 1
    home_better = ev_home >= ev_away
    return games.with_columns(
        side=pl.when(home_better).then(pl.lit(HOME)).otherwise(pl.lit(AWAY)),
        price=pl.when(home_better).then(pl.col("home_price")).otherwise(pl.col("away_price")),
        p_side=pl.when(home_better).then(p).otherwise(1 - p),
        ev=pl.when(home_better).then(ev_home).otherwise(ev_away),
        hurdle=policy.min_ev + policy.ev_per_sd * doubt(pl.col("u_sd")),
    ).with_columns(picked=pl.col("ev") >= pl.col("hurdle"))
