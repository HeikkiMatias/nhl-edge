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
from typing import TYPE_CHECKING

import numpy as np
import polars as pl

from nhl_edge.backtest.market import PREDICTION_LAG, Experiment, market_prices
from nhl_edge.backtest.metrics import log_loss
from nhl_edge.backtest.seasons import OPEN_ROLES, OPEN_SEASONS, season_role
from nhl_edge.market import recalibration
from nhl_edge.market.devig import (
    DEFAULT_METHOD,
    OVERROUND_TOLERANCE,
    Method,
    fair_probabilities,
    overround,
)

if TYPE_CHECKING:
    from nhl_edge.game import b3
    from nhl_edge.game.b2 import B2Model, Tables

# B1 recalibrates the default de-vig method's probabilities (ADR 0008).
B1_METHOD = DEFAULT_METHOD
# B2 reads no price, so it has no de-vig method.
NO_METHOD = "none"

Coverage = dict[str, dict[int, dict[str, int]]]
Fits = dict[str, dict[int, recalibration.Recalibration]]
B2Fits = dict[str, dict[int, "B2Model"]]
B3Fits = dict[str, dict[int, "b3.B3Model"]]

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


def bounds(closes: pl.DataFrame, start: datetime) -> tuple[float, float]:
    """The home probabilities E2 accepts in a fold starting at start (ADR 0007): the lowest and
    highest de-vigged home probability at every close public before start. closes holds B0 of
    E1's prices under B1_METHOD. Only closes before the fold are read and nothing is tuned, so the
    bounds are point in time, and a live E2 can apply them with the closes it has."""
    seen = closes.filter(pl.col("prediction_utc") < start)["p_home"]
    low, high = seen.min(), seen.max()
    if not (isinstance(low, float) and isinstance(high, float)):
        raise ValueError(f"no closes before {start} to bound E2's openers")
    return low, high


def implausible(prices: pl.DataFrame, low: float, high: float) -> pl.Series:
    """The openers E2 refuses (ADR 0007): a de-vigged home probability under B1_METHOD outside
    low to high, the bounds of the opener's fold, such as Edmonton -1010 against Minnesota 705
    (#56). The rule reads each market's own prices and the bounds alone. A market de-vigging
    refuses is not implausible, since it is already refused."""
    if prices.is_empty():
        return pl.Series(dtype=pl.Boolean)
    flagged = np.zeros(prices.height, dtype=bool)
    fair = ~refused(prices).to_numpy()
    if fair.any():
        pair = prices.select("home_price", "away_price").to_numpy()[fair]
        p_home = fair_probabilities(pair, B1_METHOD)[:, 0]
        flagged[fair] = (p_home < low) | (p_home > high)
    return pl.Series(flagged)


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
    it, and before the fold's first prediction when that is earlier (E2's opener). calendar holds
    the start of every game known, from the results and from the prices, so a priced game without
    a result still starts its fold. The CLI checks that games holds every game of each season it
    reads."""
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
    frame: pl.DataFrame, experiment: Experiment | str, model: str, method: Method | str
) -> pl.DataFrame:
    if "train_cutoff" not in frame.columns:
        frame = frame.with_columns(train_cutoff=pl.lit(None, PREDICTION_SCHEMA["train_cutoff"]))
    return (
        frame.with_columns(
            experiment=pl.lit(
                experiment.value if isinstance(experiment, Experiment) else experiment
            ),
            model=pl.lit(model),
            method=pl.lit(method.value if isinstance(method, Method) else method),
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
    refuse_implausible: bool = True,
    b2_tables: "Tables | None" = None,
    b2_fits: B2Fits | None = None,
    b3_tables: "b3.Tables | None" = None,
    b3_fits: B3Fits | None = None,
) -> tuple[pl.DataFrame, Coverage, Fits]:
    """Every prediction for the test seasons, scored, the coverage per experiment and season, and
    B1's fit per experiment and season. sbr_odds and games hold the test seasons and the earlier
    seasons B1 is fitted on.

    E2 refuses implausible openers, those outside the range of every close before the fold, in its
    scoring and in B1's E2 fits (ADR 0007), unless refuse_implausible is False, which reports E2
    on every opener beside it.

    Coverage counts the season's games, those with a price, those priced but without a result,
    those whose market de-vigging refuses, those scored, and B1's training games. E2's also counts
    the implausible openers it refuses, and the games whose opener differs from the close, which
    sizes how much E2's opener can have moved before its assumed time (ADR 0006).

    With b2_tables, B2 (ADR 0013) predicts the games B1 scores, at the experiment's prediction
    time, from a fit on the games before the fold, and b2_fits receives its fit per experiment and
    season. Coverage then counts the games B2 scored and those it trained on. b3_tables and
    b3_fits do the same for B3, the player layer (ADR 0023)."""
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
    history = b0(market_prices(open_odds, Experiment.E1), B1_METHOD)
    for experiment in Experiment:
        quoted = market_prices(open_odds, experiment)
        every = quoted.join(results, on="game_id")
        quoted = quoted.filter(pl.col("season").is_in(seasons))
        folds = {}
        for season in seasons:
            # E2 predicts at the opener, before the start, so its fold starts at its first
            # prediction when that comes earlier.
            first = quoted.filter(pl.col("season") == season)["prediction_utc"].min()
            folds[season] = (
                min(starts[season], first) if isinstance(first, datetime) else starts[season]
            )
        # E2 refuses the openers outside each fold's bounds, in the fold's test season and in its
        # B1 fit (ADR 0007).
        refusing = experiment is Experiment.E2 and refuse_implausible
        rejected = {
            season: every.filter(implausible(every, *bounds(history, start)))
            if refusing
            else every.clear()
            for season, start in folds.items()
        }
        # Each fold's fit starts from every opener and drops only that fold's refusals, so a fold
        # never depends on which other seasons were requested.
        market = b0(every, B1_METHOD)
        unsettled = quoted.join(results, on="game_id", how="anti")
        priced = every.filter(pl.col("season").is_in(seasons))
        moved = (
            priced.join(closes, on="game_id")
            .filter(pl.concat_list("home_price", "away_price") != pl.col("close"))
            .select("season")
        )
        tested_out = pl.concat([rejected[s].filter(pl.col("season") == s) for s in seasons])
        prices = priced.join(tested_out, on="game_id", how="anti")
        dropped = prices.filter(refused(prices))
        coverage[experiment] = {}
        fits[experiment] = {}
        for season in seasons:
            in_season = pl.col("season") == season
            fold = market.join(rejected[season], on="game_id", how="anti")
            predicted, fit = b1(fold, folds[season], season)
            frames.append(_scored(predicted, experiment, "B1", B1_METHOD))
            fits[experiment][season] = fit
            b2_counts: dict[str, int] = {}
            if b2_tables is not None:
                from nhl_edge.game import b2

                moments = predicted.select("game_id", "prediction_utc")
                b2_rows, b2_fit = b2.predictions(
                    b2_tables, moments, season, folds[season], b2.TUNED
                )
                scored_b2 = b2_rows.join(
                    predicted.select(
                        "game_id", "season", "game_date", "prediction_utc", "home_win"
                    ),
                    on="game_id",
                )
                frames.append(_scored(scored_b2, experiment, "B2", NO_METHOD))
                if b2_fits is not None:
                    b2_fits.setdefault(experiment, {})[season] = b2_fit
                b2_counts = {"b2_scored": scored_b2.height, "b2_trained_on": b2_fit.games}
            if b3_tables is not None:
                from nhl_edge.game import b3

                moments = predicted.select("game_id", "prediction_utc")
                b3_rows, b3_fit = b3.predictions(b3_tables, moments, season, folds[season])
                scored_b3 = b3_rows.join(
                    predicted.select(
                        "game_id", "season", "game_date", "prediction_utc", "home_win"
                    ),
                    on="game_id",
                )
                frames.append(_scored(scored_b3, experiment, "B3", NO_METHOD))
                if b3_fits is not None:
                    b3_fits.setdefault(experiment, {})[season] = b3_fit
                b2_counts |= {"b3_scored": scored_b3.height, "b3_trained_on": b3_fit.games}
            counts = {
                "games": games.filter(in_season).height,
                "priced": quoted.filter(in_season).height,
                "unsettled": unsettled.filter(in_season).height,
                "refused": dropped.filter(in_season).height,
            }
            if experiment is Experiment.E2:
                counts["implausible"] = rejected[season].filter(in_season).height
            counts["scored"] = counts["priced"] - sum(
                counts[key] for key in ("unsettled", "refused", "implausible") if key in counts
            )
            counts["b1_trained_on"] = fit.games
            counts |= b2_counts
            if experiment is Experiment.E2:
                counts["opener_differs_from_close"] = moved.filter(in_season).height
            coverage[experiment][season] = counts
        for method in methods:
            frames.append(_scored(b0(prices, method), experiment, "B0", method))
    return pl.concat(frames), coverage, fits


HOCKEY = "hockey"


def hockey_only(
    games: pl.DataFrame,
    seasons: Iterable[int],
    b2_tables: "Tables",
    b3_tables: "b3.Tables",
    b2_fits: B2Fits | None = None,
    b3_fits: B3Fits | None = None,
) -> tuple[pl.DataFrame, Coverage]:
    """B2 and B3 scored at the as-of time on outcomes alone, for seasons without prices (ADR
    0023): every game of the season with a result, each model fitted on the games before the
    season's first start. A prediction runs PREDICTION_LAG after the as-of time, as E2's after
    10:00 ET, so it reads the rows that became known at the as-of time. Held-out seasons are
    refused until gate 2. The predictions carry the experiment HOCKEY and no method; coverage
    counts each model's scored and training games."""
    from nhl_edge.features import team_strength as ts
    from nhl_edge.game import b2, b3

    seasons = sorted(set(seasons))
    held_out = [season for season in seasons if season_role(season) not in OPEN_ROLES]
    if held_out:
        raise ValueError(f"{held_out} are held out until gate 2 (#107)")
    results = outcomes(games.filter(pl.col("season").is_in(seasons)))
    calendar = games.select("season", "start_utc")
    frames = [pl.DataFrame(schema=PREDICTION_SCHEMA)]
    coverage: Coverage = {HOCKEY: {}}
    for season in seasons:
        moments = (
            games.filter(pl.col("season") == season)
            .select(
                "game_id",
                "season",
                "game_date",
                prediction_utc=ts.as_of(pl.col("game_date"), pl.col("start_utc")) + PREDICTION_LAG,
            )
            .join(results.select("game_id", "home_win"), on="game_id")
        )
        timing = moments.select("game_id", "prediction_utc")
        # The fold starts at its first prediction, before the first puck drop (as E2's).
        first = moments["prediction_utc"].min()
        start = fold_start(calendar, season)
        if isinstance(first, datetime):
            start = min(start, first)
        counts = {"games": games.filter(pl.col("season") == season).height}
        rows, fit2 = b2.predictions(b2_tables, timing, season, start, b2.TUNED)
        frames.append(_scored(rows.join(moments, on="game_id"), HOCKEY, "B2", NO_METHOD))
        rows3, fit3 = b3.predictions(b3_tables, timing, season, start)
        frames.append(_scored(rows3.join(moments, on="game_id"), HOCKEY, "B3", NO_METHOD))
        if b2_fits is not None:
            b2_fits.setdefault(HOCKEY, {})[season] = fit2
        if b3_fits is not None:
            b3_fits.setdefault(HOCKEY, {})[season] = fit3
        counts |= {
            "b2_scored": rows.height,
            "b2_trained_on": fit2.games,
            "b3_scored": rows3.height,
            "b3_trained_on": fit3.games,
        }
        coverage[HOCKEY][season] = counts
    return pl.concat(frames), coverage
