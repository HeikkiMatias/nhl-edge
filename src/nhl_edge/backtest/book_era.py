"""Whether SBR's change of closing book matters (#65): a diagnostic reported beside the backtest,
never an input to it.

From the first game of 2018-19, SBR's closes carry about 2.3% vig instead of 3.8%, while its
openers stay at about 4% (the #9 audit): the closes likely come from a sharper book from then on.
The owner kept the closes as they are (2026-09-30), and this diagnostic shows whether it matters:
- E2 against E1. Before 2018-19 one book set both prices, so B0's E2 minus E1 there is the cost of
  predicting at the opener alone. From 2018-19 on it also holds the change of book.
- B1 in each era. Every fold's B1 is fitted mostly on the old book's closes. B1 is fitted on each
  era's closes, and on its openers as a control, to see whether the recalibration differs.

The era fits read every game of the era with hindsight, development seasons included, so no
prediction or fold reads them. An era comparison also holds whatever else changed between the
seasons.

E2 refuses implausible openers as the backtest does (ADR 0007), each season taken as its own fold:
an opener outside the range of every close before the season's first prediction is refused. The
first season has no earlier close, so it keeps every opener.
"""

from datetime import datetime
from typing import Any

import numpy as np
import polars as pl

from nhl_edge.backtest.market import Experiment, market_prices
from nhl_edge.backtest.metrics import (
    DRAWS,
    SEED,
    bootstrap,
    difference,
    interval,
    log_loss,
    resampled_fits,
)
from nhl_edge.backtest.seasons import OPEN_SEASONS
from nhl_edge.backtest.walk_forward import (
    B1_METHOD,
    b0,
    bounds,
    fold_start,
    implausible,
    outcomes,
)
from nhl_edge.market import recalibration

# The first season whose closes come from the sharper book (the #9 audit).
SWITCH_SEASON = 20182019
SAME_BOOK, NEW_CLOSING_BOOK = "same_book", "new_closing_book"
DESCRIPTION = (
    "SBR's closes come from a lower-vig book from 2018-19 on, its openers from the same book "
    "throughout (#65). B0's E2 minus E1 and B1 fitted on each era: the fits read each era's "
    "games with hindsight, development seasons included, so no prediction or fold reads them, "
    "and an era comparison also holds whatever else changed between the seasons. E2 refuses "
    "implausible openers as in ADR 0007, each season taken as its own fold; the first season has "
    "no earlier close, so it keeps every opener. Differences are the new closing book minus the "
    "same book."
)


def era(season: pl.Expr) -> pl.Expr:
    return (
        pl.when(season < SWITCH_SEASON).then(pl.lit(SAME_BOOK)).otherwise(pl.lit(NEW_CLOSING_BOOK))
    )


def markets(sbr_odds: pl.DataFrame, games: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """B0 of every open season's closes and of its plausible openers, under B1_METHOD, each game
    with its result. A season's opener is refused when it is outside the range of every close
    before the season's first prediction (ADR 0007)."""
    odds = sbr_odds.filter(pl.col("season").is_in(OPEN_SEASONS))
    results = outcomes(games.filter(pl.col("season").is_in(OPEN_SEASONS)))
    closes = b0(market_prices(odds, Experiment.E1), B1_METHOD)
    calendar = pl.concat([frame.select("season", "start_utc") for frame in (games, odds)])
    quoted = market_prices(odds, Experiment.E2)
    kept = [quoted.clear()]
    for (season,), prices in quoted.sort("season").group_by("season", maintain_order=True):
        first = prices["prediction_utc"].min()
        assert isinstance(first, datetime) and isinstance(season, int)
        start = min(fold_start(calendar, season), first)
        if closes.filter(pl.col("prediction_utc") < start).height:
            prices = prices.filter(~implausible(prices, *bounds(closes, start)))
        kept.append(prices)
    openers = b0(pl.concat(kept), B1_METHOD)
    return closes.join(results, on="game_id"), openers.join(results, on="game_id")


def _recalibration(frame: pl.DataFrame) -> dict[str, float]:
    cutoff = frame["result_utc"].max()
    assert isinstance(cutoff, datetime)
    fit = recalibration.fit(frame["p_home"], frame["home_win"], cutoff)
    return {"intercept": fit.intercept, "slope": fit.slope}


def _b1(frame: pl.DataFrame, eras: list[str], draws: int) -> dict[str, Any]:
    """B1 fitted on each era's games, with weekly block bootstrap intervals from refits, and the
    slope's difference between the eras."""
    rng = np.random.default_rng(SEED)
    fits = {}
    samples = {}
    for name in eras:
        games = frame.filter(pl.col("era") == name)
        samples[name] = resampled_fits(games, _recalibration, rng, draws)
        point = _recalibration(games)
        fits[name] = {
            "games": games.height,
            "seasons": sorted(games["season"].unique().to_list()),
            **{key: interval(value, samples[name][key]).to_dict() for key, value in point.items()},
        }
    result: dict[str, Any] = {"eras": fits}
    if len(eras) == 2:
        same, new = (fits[name]["slope"]["value"] for name in eras)
        drawn = samples[NEW_CLOSING_BOOK]["slope"] - samples[SAME_BOOK]["slope"]
        result["slope_difference"] = interval(new - same, drawn).to_dict()
    return result


def diagnostic(sbr_odds: pl.DataFrame, games: pl.DataFrame, draws: int = DRAWS) -> dict[str, Any]:
    """B0's E2 minus E1 per season and era, and B1 fitted on each era's closes and openers, over
    the open seasons sbr_odds and games hold."""
    closes, openers = (
        frame.with_columns(era=era(pl.col("season"))) for frame in markets(sbr_odds, games)
    )
    paired = (
        closes.select(
            "game_id",
            "season",
            "game_date",
            "era",
            e1=log_loss(pl.col("p_home"), pl.col("home_win")),
        )
        .join(
            openers.select("game_id", e2=log_loss(pl.col("p_home"), pl.col("home_win"))),
            on="game_id",
        )
        .with_columns(difference=pl.col("e2") - pl.col("e1"))
        .sort("season", "game_id")
    )
    eras = [name for name in (SAME_BOOK, NEW_CLOSING_BOOK) if name in set(paired["era"])]
    against: dict[str, Any] = {
        "per_season": {
            str(season): bootstrap(rows, "difference", draws).to_dict()
            for (season,), rows in paired.group_by("season", maintain_order=True)
        },
        "eras": {
            name: bootstrap(paired.filter(pl.col("era") == name), "difference", draws).to_dict()
            for name in eras
        },
    }
    if len(eras) == 2:
        against["era_difference"] = difference(
            paired.filter(pl.col("era") == NEW_CLOSING_BOOK),
            paired.filter(pl.col("era") == SAME_BOOK),
            "difference",
            draws,
        ).to_dict()
    return {
        "description": DESCRIPTION,
        "switch_season": SWITCH_SEASON,
        "method": B1_METHOD.value,
        "b0_e2_against_e1": against,
        "b1_on_closes": _b1(closes, eras, draws),
        "b1_on_openers": _b1(openers, eras, draws),
    }
