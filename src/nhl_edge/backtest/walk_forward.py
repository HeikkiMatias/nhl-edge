"""The walk-forward backtest (docs/plan.md section 5, validation design): each test season is
predicted by models fitted only on games before its first start, and every fitted component is
refit per fold.

Phase 1 has the market baselines, for E1 (the close) and E2 (the opener):
- B0, the de-vigged market price with nothing fitted, under each de-vig method so the default
  can be chosen (#10).
- B1, the recalibrated market (market/recalibration.py). Each test season's fit uses the
  experiment's own prices of the earlier open seasons, on games whose results were public before
  the season's first start, and recalibrates B1_METHOD's probabilities.

Outcomes settle the moneyline on the full game, overtime and shootout included (hard rule 2). They
are read to score a prediction and, for games before the fold, to fit B1.
"""

from collections.abc import Iterable
from datetime import datetime

import numpy as np
import polars as pl

from nhl_edge.backtest.market import Experiment, market_prices
from nhl_edge.backtest.metrics import log_loss
from nhl_edge.backtest.seasons import OPEN_ROLES, OPEN_SEASONS, season_role
from nhl_edge.market import recalibration
from nhl_edge.market.devig import OVERROUND_TOLERANCE, Method, fair_probabilities, overround

# B1 recalibrates this method's probabilities, until an ADR chooses the default de-vig method.
B1_METHOD = Method.MULTIPLICATIVE

Coverage = dict[str, dict[int, dict[str, int]]]
Fits = dict[str, dict[int, recalibration.Recalibration]]

PREDICTION_SCHEMA = {
    "experiment": pl.String,
    "model": pl.String,
    "method": pl.String,
    "season": pl.Int32,
    "game_id": pl.Int64,
    "game_date": pl.Date,
    "prediction_utc": pl.Datetime("us", "UTC"),
    "train_cutoff": pl.Datetime("us", "UTC"),
    "p_home": pl.Float64,
    "home_win": pl.Int8,
    "log_loss": pl.Float64,
}


def outcomes(games: pl.DataFrame) -> pl.DataFrame:
    """Each game's moneyline result: 1 when the home team won the full game, OT and SO included
    (games' scores count the shootout winner's goal), and when that result became public."""
    return games.select(
        "game_id",
        home_win=(pl.col("home_score") > pl.col("away_score")).cast(pl.Int8),
        result_utc=pl.col("observed_utc"),
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


def fold_start(calendar: pl.DataFrame, season: int) -> datetime:
    """The test season's first start: every game a fold's fit reads has its result public before
    it. calendar holds the start of every game known, from the results and from the prices, so a
    priced game without a result still starts its fold. The CLI checks that games holds every game
    of each season it reads."""
    first = calendar.filter(pl.col("season") == season)["start_utc"].min()
    if not isinstance(first, datetime):
        raise ValueError(f"no games of {season} to start its fold")
    return first


def b1(
    market: pl.DataFrame, start: datetime, season: int
) -> tuple[pl.DataFrame, recalibration.Recalibration]:
    """B1 for one test season. market holds B0's p_home under B1_METHOD for the season and the
    earlier ones, with each game's result and when it became public. The fit reads only games of
    earlier open seasons whose results were public before start, and its train_cutoff is the last
    of those times."""
    history = market.filter(
        pl.col("season") < season,
        pl.col("season").is_in(OPEN_SEASONS),
        pl.col("result_utc") < start,
    )
    if history.is_empty():
        raise ValueError(f"no games before {season} to fit B1 on")
    cutoff = history["result_utc"].max()
    assert isinstance(cutoff, datetime)
    model = recalibration.fit(history["p_home"], history["home_win"], cutoff)
    tested = market.filter(pl.col("season") == season)
    return (
        tested.with_columns(
            p_home=pl.Series(model.predict(tested["p_home"].to_numpy()), dtype=pl.Float64),
            train_cutoff=pl.lit(cutoff, PREDICTION_SCHEMA["train_cutoff"]),
        ),
        model,
    )


def _scored(
    frame: pl.DataFrame, experiment: Experiment, model: str, method: Method
) -> pl.DataFrame:
    if "train_cutoff" not in frame.columns:
        frame = frame.with_columns(train_cutoff=pl.lit(None, PREDICTION_SCHEMA["train_cutoff"]))
    return (
        frame.with_columns(
            experiment=pl.lit(experiment.value),
            model=pl.lit(model),
            method=pl.lit(method.value),
            log_loss=log_loss(pl.col("p_home"), pl.col("home_win")),
        )
        .select(list(PREDICTION_SCHEMA))
        .cast(PREDICTION_SCHEMA)  # type: ignore[arg-type]
    )


def run(
    sbr_odds: pl.DataFrame,
    games: pl.DataFrame,
    seasons: Iterable[int],
    methods: Iterable[Method] = tuple(Method),
) -> tuple[pl.DataFrame, Coverage, Fits]:
    """Every prediction for the test seasons, scored, the coverage per experiment and season, and
    B1's fit per experiment and season. sbr_odds and games hold the test seasons and the earlier
    seasons B1 is fitted on.

    Coverage counts the season's games, those with a price, those priced but without a result,
    those whose market de-vigging refuses, those scored, and B1's training games. E2's also counts
    the games whose opener differs from the close, which sizes how much E2's opener can have moved
    before its assumed time (ADR 0006)."""
    seasons = sorted(set(seasons))
    held_out = [season for season in seasons if season_role(season) not in OPEN_ROLES]
    if held_out:
        raise ValueError(f"{held_out} are held out: phase 1 backtests open seasons only (#10)")
    open_odds = sbr_odds.filter(pl.col("season").is_in(OPEN_SEASONS))
    results = outcomes(games.filter(pl.col("season").is_in(OPEN_SEASONS)))
    calendar = pl.concat([frame.select("season", "start_utc") for frame in (games, sbr_odds)])
    starts = {season: fold_start(calendar, season) for season in seasons}
    frames = [pl.DataFrame(schema=PREDICTION_SCHEMA)]
    coverage: Coverage = {}
    fits: Fits = {}
    tested = open_odds.filter(pl.col("season").is_in(seasons))
    closes = market_prices(tested, Experiment.E1).select(
        "game_id", close=pl.concat_list("home_price", "away_price")
    )
    for experiment in Experiment:
        quoted = market_prices(open_odds, experiment)
        every = quoted.join(results, on="game_id")
        market = b0(every, B1_METHOD)
        quoted = quoted.filter(pl.col("season").is_in(seasons))
        unsettled = quoted.join(results, on="game_id", how="anti")
        prices = every.filter(pl.col("season").is_in(seasons))
        dropped = prices.filter(refused(prices))
        moved = (
            prices.join(closes, on="game_id")
            .filter(pl.concat_list("home_price", "away_price") != pl.col("close"))
            .select("season")
        )
        coverage[experiment] = {}
        fits[experiment] = {}
        for season in seasons:
            predicted, fit = b1(market, starts[season], season)
            frames.append(_scored(predicted, experiment, "B1", B1_METHOD))
            fits[experiment][season] = fit
            in_season = pl.col("season") == season
            counts = {
                "games": games.filter(in_season).height,
                "priced": quoted.filter(in_season).height,
                "unsettled": unsettled.filter(in_season).height,
                "refused": dropped.filter(in_season).height,
            }
            counts["scored"] = counts["priced"] - counts["unsettled"] - counts["refused"]
            counts["b1_trained_on"] = fit.games
            if experiment is Experiment.E2:
                counts["opener_differs_from_close"] = moved.filter(in_season).height
            coverage[experiment][season] = counts
        for method in methods:
            frames.append(_scored(b0(prices, method), experiment, "B0", method))
    return pl.concat(frames), coverage, fits
