"""The walk-forward backtest (docs/plan.md section 5, validation design): each test season is
predicted by models fitted only on games before its first start, and every fitted component is
refit per fold.

Phase 1 has the market baseline B0, the de-vigged market price with nothing fitted, for E1 (the
close) and E2 (the opener), under each de-vig method so the default can be chosen (#10). Outcomes
settle the moneyline on the full game, overtime and shootout included (hard rule 2), and are read
only to score a prediction.
"""

from collections.abc import Iterable

import numpy as np
import polars as pl

from nhl_edge.backtest.market import Experiment, market_prices
from nhl_edge.backtest.metrics import log_loss
from nhl_edge.backtest.seasons import OPEN_ROLES, season_role
from nhl_edge.market.devig import OVERROUND_TOLERANCE, Method, fair_probabilities, overround

PREDICTION_SCHEMA = {
    "experiment": pl.String,
    "model": pl.String,
    "method": pl.String,
    "season": pl.Int32,
    "game_id": pl.Int64,
    "game_date": pl.Date,
    "prediction_utc": pl.Datetime("us", "UTC"),
    "p_home": pl.Float64,
    "home_win": pl.Int8,
    "log_loss": pl.Float64,
}


def outcomes(games: pl.DataFrame) -> pl.DataFrame:
    """Each game's moneyline result: 1 when the home team won the full game, OT and SO included
    (games' scores count the shootout winner's goal)."""
    return games.select(
        "game_id", home_win=(pl.col("home_score") > pl.col("away_score")).cast(pl.Int8)
    )


def refused(prices: pl.DataFrame) -> pl.Series:
    """The markets de-vigging refuses: prices whose implied probabilities sum below 100%, which
    one book's prices for one market never do (such as both sides at plus money)."""
    if prices.is_empty():
        return pl.Series(dtype=pl.Boolean)
    total = overround(prices.select("home_price", "away_price").to_numpy())
    return pl.Series(total < 1 - OVERROUND_TOLERANCE)


def b0(prices: pl.DataFrame, method: Method) -> pl.DataFrame:
    """B0: the de-vigged home probability of each market de-vigging accepts."""
    fair = prices.filter(~refused(prices))
    p_home = (
        fair_probabilities(fair.select("home_price", "away_price").to_numpy(), method)[:, 0]
        if fair.height
        else np.empty(0)
    )
    return fair.with_columns(p_home=pl.Series(p_home, dtype=pl.Float64))


def run(
    sbr_odds: pl.DataFrame,
    games: pl.DataFrame,
    seasons: Iterable[int],
    methods: Iterable[Method] = tuple(Method),
) -> tuple[pl.DataFrame, dict[str, dict[int, dict[str, int]]]]:
    """Every prediction for the test seasons, scored, and the coverage per experiment and season:
    the season's games, those with a price, those whose market de-vigging refuses, and those
    scored. E2's coverage also counts the games whose opener differs from the close, which sizes
    how much E2's opener can have moved before its assumed time (ADR 0006)."""
    seasons = list(seasons)
    held_out = [season for season in seasons if season_role(season) not in OPEN_ROLES]
    if held_out:
        raise ValueError(f"{held_out} are held out: phase 1 backtests open seasons only (#10)")
    results = outcomes(games.filter(pl.col("season").is_in(seasons)))
    frames = [pl.DataFrame(schema=PREDICTION_SCHEMA)]
    coverage: dict[str, dict[int, dict[str, int]]] = {}
    tested = sbr_odds.filter(pl.col("season").is_in(seasons))
    closes = market_prices(tested, Experiment.E1).select(
        "game_id", close=pl.concat_list("home_price", "away_price")
    )
    for experiment in Experiment:
        prices = market_prices(tested, experiment).join(results, on="game_id")
        dropped = prices.filter(refused(prices))
        moved = (
            prices.join(closes, on="game_id")
            .filter(pl.concat_list("home_price", "away_price") != pl.col("close"))
            .select("season")
        )
        coverage[experiment] = {}
        for season in seasons:
            in_season = pl.col("season") == season
            counts = {
                "games": games.filter(in_season).height,
                "priced": prices.filter(in_season).height,
                "refused": dropped.filter(in_season).height,
            }
            counts["scored"] = counts["priced"] - counts["refused"]
            if experiment is Experiment.E2:
                counts["opener_differs_from_close"] = moved.filter(in_season).height
            coverage[experiment][season] = counts
        for method in methods:
            frames.append(
                b0(prices, method)
                .with_columns(
                    experiment=pl.lit(experiment.value),
                    model=pl.lit("B0"),
                    method=pl.lit(method.value),
                    log_loss=log_loss(pl.col("p_home"), pl.col("home_win")),
                )
                .select(list(PREDICTION_SCHEMA))
                .cast(PREDICTION_SCHEMA)  # type: ignore[arg-type]
            )
    return pl.concat(frames), coverage
