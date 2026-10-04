"""The market blend in the walk-forward (#140, ADR 0027).

Each tested season's blend learns from the out-of-sample predictions of the folds before it
(seasons.blend_training_seasons, hard rule 6): each earlier fold's own scored games, with B2's
and B3's predictions from fits cut off before that fold, B0's de-vigged market price under
B1_METHOD, u's parts, and the full-game result, which must be public before the tested season's
fold starts. Every row a fit reads must be known before the fold starts, or the fold is refused.
The first out-of-sample season has no earlier fold, so it has no blend.

Three fits per experiment and season, on the same training games:
- BLEND, on B3 (ADR 0024): the model the policy bets with;
- BLEND_B2, on B2: B3's reference at the blend level (hard rule 3);
- BLEND_MARKET, the market alone (b_x = b_u = 0): the control that shows how much of a gain is
  only the market recalibrated on these games (2021-22's market was itself under-confident).

u is standardized on the training games (uncertainty.fit_scale). The predictions join the other
models' under the experiment, so reports.experiment pairs each against B1.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from nhl_edge.backtest import b2_report
from nhl_edge.backtest.metrics import bootstrap, log_loss
from nhl_edge.backtest.seasons import blend_training_seasons
from nhl_edge.backtest.walk_forward import B1_METHOD, PREDICTION_SCHEMA, outcomes
from nhl_edge.game import uncertainty
from nhl_edge.market import blend
from nhl_edge.market.blend import Kind

BLEND = "BLEND"
BLEND_B2 = "BLEND_B2"
BLEND_MARKET = "BLEND_MARKET"
# Each blend's hockey model, or none for the control.
MODELS: dict[str, str | None] = {BLEND: "B3", BLEND_B2: "B2", BLEND_MARKET: None}
BASELINE = "B1"
GAP = b2_report.GAP
GAPS_FILE = "gaps_blend.csv"
# A season's opening weeks, reported apart (#13, finding 11).
EARLY_DAYS = 28

Fits = dict[str, dict[int, dict[str, blend.Blend]]]
Scales = dict[str, dict[int, uncertainty.Scale]]


def rows(
    predictions: pl.DataFrame, parts: dict[str, pl.DataFrame], games: pl.DataFrame
) -> pl.DataFrame:
    """One row per experiment and game that B0, B2 and B3 all predicted and u covers: the market's
    de-vigged probability (p_mkt), B2's and B3's (p_b2, p_b3) with their fits' cutoffs, u's parts
    with their times, and the result with when it became public."""

    def model(name: str, column: str) -> pl.DataFrame:
        return predictions.filter(pl.col("model") == name).select(
            "experiment",
            "game_id",
            pl.col("p_home").alias(column),
            **{f"{column}_cutoff": "train_cutoff"},
        )

    market = predictions.filter(
        pl.col("model") == "B0", pl.col("method") == B1_METHOD.value
    ).select(
        "experiment", "season", "game_id", "game_date", "prediction_utc", "home_win", p_mkt="p_home"
    )
    doubts = pl.concat(
        [
            frame.select(
                pl.lit(experiment).alias("experiment"),
                "game_id",
                *uncertainty.PARTS,
                parts_utc="observed_utc",
                parts_cutoff="train_cutoff",
            )
            for experiment, frame in parts.items()
        ]
    )
    return (
        market.join(model("B2", "p_b2"), on=["experiment", "game_id"])
        .join(model("B3", "p_b3"), on=["experiment", "game_id"])
        .join(doubts, on=["experiment", "game_id"])
        .join(outcomes(games).select("game_id", "result_utc"), on="game_id")
        .sort("experiment", "game_id")
    )


def _known_by(frame: pl.DataFrame) -> datetime:
    """The latest moment any input of frame's rows became known or was cut off."""
    latest = frame.select(
        pl.max_horizontal(
            "result_utc", "p_b2_cutoff", "p_b3_cutoff", "parts_utc", "parts_cutoff"
        ).max()
    ).item()
    assert isinstance(latest, datetime)
    return latest


def run(
    every: pl.DataFrame, starts: dict[tuple[str, int], datetime], tested: list[int]
) -> tuple[pl.DataFrame, Fits, Scales, dict[str, dict[int, dict[str, int]]]]:
    """The blends' predictions for the tested seasons, scored, their fits and u scales per
    experiment and season, and their coverage: the games scored and trained on. every holds
    rows() for the tested seasons and the folds before them."""
    frames = [pl.DataFrame(schema=PREDICTION_SCHEMA)]
    fits: Fits = {}
    scales: Scales = {}
    coverage: dict[str, dict[int, dict[str, int]]] = {}
    for (experiment,), by_experiment in every.group_by("experiment", maintain_order=True):
        experiment = str(experiment)
        for season in tested:
            learned = blend_training_seasons(season)
            if not learned:
                continue  # the first out-of-sample season has no earlier fold
            start = starts[(experiment, season)]
            train = by_experiment.filter(
                pl.col("season").is_in(learned),
                pl.col("result_utc") < start,
                pl.col("prediction_utc") < start,
            )
            if train.is_empty():
                raise ValueError(f"no out-of-sample games before {season} to fit the blend on")
            known = _known_by(train)
            if known >= start:
                raise ValueError(
                    f"the {experiment} blend of {season} would read a row known at {known}, "
                    f"after its fold starts at {start}"
                )
            scale = uncertainty.fit_scale(
                train.select(
                    *uncertainty.PARTS, observed_utc="parts_utc", train_cutoff="parts_cutoff"
                )
            )
            u_train = scale.score(train).to_numpy()
            y = train["home_win"].to_numpy()
            p_mkt = train["p_mkt"].to_numpy()
            fitted = {
                name: blend.fit(
                    Kind.MARKET if source is None else Kind.MODEL,
                    y,
                    p_mkt,
                    known,
                    None if source is None else train[f"p_{source.lower()}"].to_numpy(),
                    None if source is None else u_train,
                )
                for name, source in MODELS.items()
            }
            tested_rows = by_experiment.filter(pl.col("season") == season)
            # The scored games' own inputs were cut off before the fold too (B2's and B3's fits,
            # and the fitted tables behind u).
            scored_cutoff = tested_rows.select(
                pl.max_horizontal("p_b2_cutoff", "p_b3_cutoff", "parts_cutoff").max()
            ).item()
            if isinstance(scored_cutoff, datetime) and scored_cutoff >= start:
                raise ValueError(
                    f"the {experiment} blend of {season} would score a game whose inputs were "
                    f"cut off at {scored_cutoff}, after its fold starts at {start}"
                )
            u_test = scale.score(tested_rows).to_numpy()
            for name, source in MODELS.items():
                model = fitted[name]
                p = model.predict(
                    tested_rows["p_mkt"].to_numpy(),
                    None if source is None else tested_rows[f"p_{source.lower()}"].to_numpy(),
                    None if source is None else u_test,
                )
                # Each row was known by its own prediction: the fit's cutoff, and its hockey
                # model's and u's for that game, all before the fold.
                own = [model.train_cutoff, scale.train_cutoff]
                cutoff = tested_rows.select(
                    pl.max_horizontal(
                        pl.lit(max(own)), "parts_cutoff", "p_b2_cutoff", "p_b3_cutoff"
                    ).alias("train_cutoff")
                )["train_cutoff"]
                frames.append(
                    tested_rows.select(
                        "season", "game_id", "game_date", "prediction_utc", "home_win"
                    ).with_columns(
                        experiment=pl.lit(experiment),
                        model=pl.lit(name),
                        method=pl.lit(B1_METHOD.value),
                        p_home=pl.Series(p, dtype=pl.Float64),
                        train_cutoff=cutoff,
                    )
                )
            fits.setdefault(experiment, {})[season] = fitted
            scales.setdefault(experiment, {})[season] = scale
            coverage.setdefault(experiment, {})[season] = {
                "blend_scored": tested_rows.height,
                "blend_trained_on": train.height,
            }
    scored = pl.concat(
        [
            frame.with_columns(log_loss=log_loss(pl.col("p_home"), pl.col("home_win")))
            .select(list(PREDICTION_SCHEMA))
            .cast(PREDICTION_SCHEMA)  # type: ignore[arg-type]
            for frame in frames
        ]
    )
    return scored, fits, scales, coverage


def _estimates(frame: pl.DataFrame, value: str) -> dict[str, Any]:
    if frame.is_empty():
        return {"pooled": None, "per_season": {}}
    return {
        "pooled": bootstrap(frame, value).to_dict(),
        "per_season": {
            str(season): bootstrap(rows, value).to_dict()
            for (season,), rows in frame.sort("season").group_by("season", maintain_order=True)
        },
    }


def paired(rows: pl.DataFrame, model: str, reference: str) -> pl.DataFrame:
    """Per game, model's log loss less reference's, on the games both scored."""
    keys = ["season", "game_id", "game_date"]
    return (
        rows.filter(pl.col("model") == model)
        .select(*keys, "log_loss")
        .join(
            rows.filter(pl.col("model") == reference).select("game_id", other="log_loss"),
            on="game_id",
        )
        .with_columns(difference=pl.col("log_loss") - pl.col("other"))
    )


def groups(every: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """Per experiment and game, the attribution groups of plan §10 (#13, findings 4 and 11):
    whether B3 and the market pick different favourites, and whether the game falls in its
    season's first EARLY_DAYS days. Inputs only: no result is read."""
    opening = games.group_by("season").agg(first_day=pl.col("game_date").min())
    return every.join(opening, on="season").select(
        "experiment",
        "game_id",
        different_favourites=(pl.col("p_b3") - 0.5) * (pl.col("p_mkt") - 0.5) < 0,
        early_season=(pl.col("game_date") - pl.col("first_day")).dt.total_days() < EARLY_DAYS,
    )


def gap_rows(rows: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """The games where the blend differs from B1 by more than GAP, without results."""
    keys = ["season", "game_id", "game_date"]
    return (
        rows.filter(pl.col("model") == BLEND)
        .select(*keys, p_blend="p_home")
        .join(
            rows.filter(pl.col("model") == BASELINE).select("game_id", p_b1="p_home"),
            on="game_id",
        )
        .with_columns(gap=pl.col("p_blend") - pl.col("p_b1"))
        .filter(pl.col("gap").abs() > GAP)
        .join(games.select("game_id", "home", "away"), on="game_id")
        .select("season", "game_id", "game_date", "home", "away", "p_blend", "p_b1", "gap")
        .sort("game_date", "game_id")
    )


def write_gaps(predictions: pl.DataFrame, games: pl.DataFrame, out: Path) -> Path:
    """Every experiment's blend gaps above GAP to out/gaps_blend.csv, for manual review (hard
    rule 8)."""
    frames = [
        gap_rows(predictions.filter(pl.col("experiment") == experiment), games).select(
            pl.lit(experiment).alias("experiment"), pl.all()
        )
        for experiment in sorted(predictions["experiment"].unique().to_list())
    ]
    path = out / GAPS_FILE
    pl.concat(frames).with_columns(pl.col("p_blend", "p_b1", "gap").round(4)).write_csv(path)
    return path


def fit_rows(
    fits: dict[int, dict[str, blend.Blend]], scales: dict[int, uncertainty.Scale], name: str
) -> dict[str, Any]:
    return {
        str(season): {
            "kind": by_name[name].kind.value,
            "weights": by_name[name].named(),
            "standard_errors": dict(
                zip(by_name[name].named(), by_name[name].standard_errors, strict=True)
            ),
            "games": by_name[name].games,
            "train_cutoff": by_name[name].train_cutoff.isoformat(),
            "u_scale": {
                "means": dict(zip(uncertainty.PARTS, scales[season].means, strict=True)),
                "sds": dict(zip(uncertainty.PARTS, scales[season].sds, strict=True)),
            },
        }
        for season, by_name in sorted(fits.items())
    }


def add(
    report: dict[str, Any],
    predictions: pl.DataFrame,
    fits: Fits,
    scales: Scales,
    coverage: dict[str, dict[int, dict[str, int]]],
    every: pl.DataFrame,
    games: pl.DataFrame,
) -> dict[str, Any]:
    """The report with each blend's fits, the B3 blend's paired differences against the B2 blend
    and the market-only control, its calibration, its gaps above GAP against B1 and its
    attribution groups, and the blends' coverage beside the experiment's."""
    flags = groups(every, games)
    for experiment, body in report["experiments"].items():
        for season, counts in coverage.get(experiment, {}).items():
            body["coverage"].setdefault(str(season), {}).update(counts)
        models = body["models"]
        rows = predictions.filter(pl.col("experiment") == experiment)
        for name in MODELS:
            if name in models:
                models[name]["fits"] = fit_rows(
                    fits.get(experiment, {}), scales.get(experiment, {}), name
                )
        results = models.get(BLEND)
        if results is None:
            continue
        results["paired_against_BLEND_B2"] = _estimates(paired(rows, BLEND, BLEND_B2), "difference")
        results["paired_against_BLEND_MARKET"] = _estimates(
            paired(rows, BLEND, BLEND_MARKET), "difference"
        )
        scored = rows.filter(pl.col("model") == BLEND)
        results["calibration"] = b2_report.calibrated(scored)
        compared = scored.join(
            rows.filter(pl.col("model") == BASELINE).select("game_id"), on="game_id"
        )
        flagged = gap_rows(rows, games)
        counts = dict(flagged.group_by("season").len().iter_rows())
        results["gaps_over_8_points"] = {
            "threshold": GAP,
            "games_compared": compared.height,
            "count": flagged.height,
            "per_season": {str(s): counts.get(s, 0) for s in sorted(compared["season"].unique())},
            "listed_in": GAPS_FILE,
        }
        against = paired(rows, BLEND, BASELINE).join(
            flags.filter(pl.col("experiment") == experiment).drop("experiment"), on="game_id"
        )
        results["attribution"] = {
            "different_favourites": _estimates(
                against.filter(pl.col("different_favourites")), "difference"
            ),
            "same_favourite": _estimates(
                against.filter(~pl.col("different_favourites")), "difference"
            ),
            "first_28_days": _estimates(against.filter(pl.col("early_season")), "difference"),
            "after_28_days": _estimates(against.filter(~pl.col("early_season")), "difference"),
        }
    return report
