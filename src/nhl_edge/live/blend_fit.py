"""The live blend (ADR 0030, docs/plans/phase-5.md task 2): the one fit the frozen policy bets
with in 2026-27, fitted once before the season and never refitted on live games.

**Training rows.** E1's out-of-sample rows of every priced fold before the live season: 2018-19 to
2021-22, and 2022-23's 342 SBR-priced games (ADR 0030). For each season, B0 is the close
de-vigged under B1_METHOD at puck drop, and B2 and B3 predict at that moment from fits cut off
before the season's fold start, as in the walk-forward. u's parts are read at the same moments.
The rows are rebuilt here because no run kept them per game.

**Never scored.** 2022-23 was spent by its one run (ADR 0025). This path computes no log loss,
calibration, gap, bet or CLV, and writes nothing under reports/backtest/; each game's result is
only a training label, as every earlier fold's is. It is separate from walk_forward.run, whose
refusals stand. The owner ruled on 2026-10-05 that it is not a second run.

**One fit.** u's scale, then BLEND and its twins BLEND_B2 and BLEND_MARKET through
backtest/blend.fit_fold, the frozen code, on rows public before the live fold start. B1's E1
recalibration for the live season is fitted on every earlier SBR close.

**The check.** The rows of 2018-19 to 2021-22 alone, at 2022-23's fold start, must reproduce the
2022-23 fold's E1 fits that the one run recorded (REFERENCE), or nothing is written.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from nhl_edge.backtest import blend as blend_backtest
from nhl_edge.backtest import one_time, walk_forward
from nhl_edge.backtest.market import Experiment, market_prices
from nhl_edge.backtest.seasons import blend_training_seasons
from nhl_edge.backtest.walk_forward import B1_METHOD, NO_METHOD, b0, outcomes
from nhl_edge.betting.selection import LIVE_BLEND_EXPERIMENT, POLICY_VERSION
from nhl_edge.game import b2, b3, uncertainty
from nhl_edge.market import blend, recalibration
from nhl_edge.market.blend import Kind

COMPONENT = "blend-live"
# Where the season's one fit is committed, and its R2 copy and claim.
REPORTS = Path("reports/live")
R2_PREFIX = "live"
CLAIM = one_time.Test(
    "the live blend fit of 2026-27",
    "ledger/live_blend_20262027.txt",
    "live_blend_20262027.txt",
    (20262027,),
)
LIVE_SEASON = 20262027
EXPERIMENT = Experiment(LIVE_BLEND_EXPERIMENT)
# Every priced fold before the live season, 2022-23 included (ADR 0030).
TRAINING_SEASONS = (20182019, 20192020, 20202021, 20212022, 20222023)
# The one run's record of the 2022-23 fold, which the rebuilt rows must reproduce.
REFERENCE = Path("reports/backtest/market-validation-20261005-6ec331b.json")
REFERENCE_SEASON = 20222023
# How close a rebuilt weight, standard error or scale must come to the recorded one: the same
# rows through the same code give the same numbers up to float summation.
TOLERANCE = 1e-9
PREDICTION_COLUMNS = {
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
}


def market(sbr_odds: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """B0 at E1 for every priced game with a result: the close de-vigged under B1_METHOD, at puck
    drop, with the result and when it became public."""
    return b0(market_prices(sbr_odds, EXPERIMENT).join(outcomes(games), on="game_id"), B1_METHOD)


def fold_start(sbr_odds: pl.DataFrame, games: pl.DataFrame, season: int) -> datetime:
    """The season's E1 fold start as the walk-forward sets it: its first start over the games and
    every price."""
    quotes = sbr_odds.filter(pl.col("season") == season)
    return walk_forward.fold_starts(quotes, games, [season], sbr_odds)[(EXPERIMENT.value, season)]


def season_predictions(
    tables: b2.Tables,
    b3_tables: b3.Tables,
    u_tables: uncertainty.Tables,
    priced: pl.DataFrame,
    season: int,
    start: datetime,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """B0, B2 and B3 for the season's priced games (market()) at E1's moments, from fits cut off
    before start, in the walk-forward's columns but never scored; and u's parts for the games B3
    predicted."""
    tested = priced.filter(pl.col("season") == season)
    if tested.is_empty():
        raise ValueError(f"no priced games of {season} with a result")
    moments = tested.select("game_id", "prediction_utc")
    base = tested.select("season", "game_id", "game_date", "prediction_utc", "home_win")
    b2_rows, _ = b2.predictions(tables, moments, season, start, b2.TUNED)
    b3_rows, _ = b3.predictions(b3_tables, moments, season, start)
    frames = [
        tested.with_columns(
            model=pl.lit("B0"), method=pl.lit(B1_METHOD.value), train_cutoff=pl.lit(None)
        ),
        b2_rows.select("game_id", "p_home", "train_cutoff")
        .join(base, on="game_id")
        .with_columns(model=pl.lit("B2"), method=pl.lit(NO_METHOD)),
        b3_rows.select("game_id", "p_home", "train_cutoff")
        .join(base, on="game_id")
        .with_columns(model=pl.lit("B3"), method=pl.lit(NO_METHOD)),
    ]
    predictions = pl.concat(
        frame.with_columns(experiment=pl.lit(EXPERIMENT.value))
        .select(list(PREDICTION_COLUMNS))
        .cast(PREDICTION_COLUMNS)  # type: ignore[arg-type]
        for frame in frames
    )
    b3_moments = predictions.filter(pl.col("model") == "B3").select("game_id", "prediction_utc")
    parts = uncertainty.parts(u_tables, b3_moments).with_columns(season=pl.lit(season))
    return predictions, parts


def early(rows: pl.DataFrame, starts: dict[int, datetime]) -> list[str]:
    """Why rows break their own folds' point in time: a row whose B2, B3 or u inputs were cut off
    at or after its season's fold start, or whose u read a row public at or after its prediction
    time. The live fit checks only the live fold start, so each season's own is checked here."""
    problems = []
    for (season,), frame in rows.group_by("season", maintain_order=True):
        start = starts[int(season)]  # type: ignore[call-overload]
        cut = frame.select(pl.max_horizontal("p_b2_cutoff", "p_b3_cutoff", "parts_cutoff").max())
        latest = cut.item()
        if isinstance(latest, datetime) and latest >= start:
            problems.append(
                f"{season}: an input cut off at {latest}, after its fold starts {start}"
            )
        late = frame.filter(pl.col("parts_utc") >= pl.col("prediction_utc")).height
        if late:
            problems.append(f"{season}: {late} games whose u read a row public at the prediction")
    return problems


@dataclass(frozen=True)
class Fold:
    """One fold's three blends, its u scale and the rows they learned from."""

    blends: dict[str, blend.Blend]
    scale: uncertainty.Scale
    train: pl.DataFrame


def fit(rows: pl.DataFrame, seasons: list[int], start: datetime, season: int) -> Fold:
    """The blends and u scale of the fold starting at start, learned from rows of seasons through
    the frozen fold fit, which refuses any row known at or after start."""
    fitted, scale, train = blend_backtest.fit_fold(
        rows.filter(pl.col("experiment") == EXPERIMENT.value),
        seasons,
        start,
        EXPERIMENT.value,
        season,
    )
    return Fold(fitted, scale, train)


def b1_fit(priced: pl.DataFrame, start: datetime) -> recalibration.Recalibration:
    """B1 for the live season: every earlier SBR close whose result was public before start."""
    history = priced.filter(pl.col("season") < LIVE_SEASON, pl.col("result_utc") < start)
    if history.is_empty():
        raise ValueError("no SBR closes before the live season to fit B1 on")
    cutoff = history["result_utc"].max()
    assert isinstance(cutoff, datetime)
    return recalibration.fit(history["p_home"], history["home_win"], cutoff)


def describe(fold: Fold) -> dict[str, Any]:
    """Each blend as the backtest reports it (weights, standard errors, games, train_cutoff, u
    scale)."""
    return {
        name: blend_backtest.fit_rows({0: fold.blends}, {0: fold.scale}, name)["0"]
        for name in blend_backtest.MODELS
    }


def differences(rebuilt: dict[str, Any], recorded: dict[str, Any]) -> list[str]:
    """Where the rebuilt fold's fits part from the recorded ones: games and train_cutoff exactly,
    weights, standard errors and the u scale within TOLERANCE."""
    problems = []
    for name in blend_backtest.MODELS:
        new, old = rebuilt[name], recorded.get(name)
        if old is None:
            problems.append(f"{name}: not recorded")
            continue
        for key in ("kind", "games", "train_cutoff"):
            if new[key] != old[key]:
                problems.append(f"{name} {key}: {new[key]} against {old[key]}")
        for group in ("weights", "standard_errors"):
            for term, value in new[group].items():
                if abs(value - old[group][term]) > TOLERANCE:
                    problems.append(f"{name} {group} {term}: {value} against {old[group][term]}")
        for group in ("means", "sds"):
            for part, value in new["u_scale"][group].items():
                if abs(value - old["u_scale"][group][part]) > TOLERANCE:
                    problems.append(f"{name} u {group} {part}: {value}")
        if abs(new["u_scale"]["u_sd"] - old["u_scale"]["u_sd"]) > TOLERANCE:
            problems.append(f"{name} u_sd: {new['u_scale']['u_sd']}")
    return problems


def recorded(path: Path = REFERENCE) -> dict[str, Any]:
    """The one run's E1 fits of the 2022-23 fold, by blend."""
    models = json.loads(path.read_text())["experiments"][EXPERIMENT.value]["models"]
    return {name: models[name]["fits"][str(REFERENCE_SEASON)] for name in blend_backtest.MODELS}


def artifact(
    version: str,
    fold: Fold,
    b1: recalibration.Recalibration,
    start: datetime,
    fitted_utc: datetime,
    reproduced: dict[str, Any],
) -> dict[str, Any]:
    """The live fit's record: what task 3's predictions read, and how it was checked."""
    train_cutoff = max(fold.scale.train_cutoff, *(b.train_cutoff for b in fold.blends.values()))
    if train_cutoff >= start:
        raise ValueError(f"the live fit read a row known at {train_cutoff}, after {start}")
    per_season = fold.train.group_by("season").len().sort("season")
    return {
        "version": version,
        "fitted_utc": fitted_utc.isoformat(),
        "policy": POLICY_VERSION,
        "season": LIVE_SEASON,
        "experiment": EXPERIMENT.value,
        "fold_start": start.isoformat(),
        "train_cutoff": train_cutoff.isoformat(),
        "training": {
            "seasons": list(TRAINING_SEASONS),
            "games": fold.train.height,
            "per_season": {str(s): n for s, n in per_season.iter_rows()},
        },
        "fits": describe(fold),
        "b1": {
            "intercept": b1.intercept,
            "slope": b1.slope,
            "games": b1.games,
            "train_cutoff": b1.train_cutoff.isoformat(),
        },
        "reproduction": {
            "season": REFERENCE_SEASON,
            "seasons": blend_training_seasons(REFERENCE_SEASON),
            "reference": str(REFERENCE),
            "tolerance": TOLERANCE,
            "fits": reproduced,
        },
        "note": "Fitted once for 2026-27 (ADR 0030); live games never refit it. 2022-23's rows "
        "were predicted, never scored: not a second run (the owner's ruling, 2026-10-05).",
    }


@dataclass(frozen=True)
class LiveFit:
    """The live fit as task 3 reads it back."""

    version: str
    blends: dict[str, blend.Blend]
    scale: uncertainty.Scale
    b1: recalibration.Recalibration
    fold_start: datetime
    train_cutoff: datetime


def load(record: dict[str, Any]) -> LiveFit:
    """A LiveFit from its artifact()."""
    first = next(iter(record["fits"].values()))
    scale = uncertainty.Scale(
        means=tuple(first["u_scale"]["means"][p] for p in uncertainty.PARTS),
        sds=tuple(first["u_scale"]["sds"][p] for p in uncertainty.PARTS),
        games=record["training"]["games"],
        train_cutoff=datetime.fromisoformat(record["train_cutoff"]),
        u_sd=first["u_scale"]["u_sd"],
    )
    # By name: the file's keys are sorted, which is not the order the blend's terms are fitted in.
    blends = {
        name: blend.Blend(
            kind=Kind(entry["kind"]),
            weights=tuple(entry["weights"][t] for t in blend.TERMS[Kind(entry["kind"])]),
            standard_errors=tuple(
                entry["standard_errors"][t] for t in blend.TERMS[Kind(entry["kind"])]
            ),
            games=entry["games"],
            train_cutoff=datetime.fromisoformat(entry["train_cutoff"]),
        )
        for name, entry in record["fits"].items()
    }
    b1 = recalibration.Recalibration(
        intercept=record["b1"]["intercept"],
        slope=record["b1"]["slope"],
        games=record["b1"]["games"],
        train_cutoff=datetime.fromisoformat(record["b1"]["train_cutoff"]),
    )
    return LiveFit(
        version=record["version"],
        blends=blends,
        scale=scale,
        b1=b1,
        fold_start=datetime.fromisoformat(record["fold_start"]),
        train_cutoff=datetime.fromisoformat(record["train_cutoff"]),
    )
