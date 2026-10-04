"""B3's part of the backtest report (#106, ADR 0023), beside its log loss and its paired
difference against B1, which reports.experiment gives every model:
- its fits, one per experiment and season, with their weights and train_cutoff;
- its paired difference against B2 (hard rule 3), pooled and per season, and on each of gate 2's
  subsets (backtest.subsets) and their union, without an interval where a season's games fall
  in one week;
- its calibration intercept and slope;
- how many games differ from B1 by more than GAP (hard rule 8), and the games themselves in
  gaps_b3.csv for manual review, without their results;
- lineup quality on the games it scored: the goalie-start model's Brier score, and the
  projection's 5v5 minutes error and power-play unit accuracy (ADR 0018).

The hockey-only mode (ADR 0023) reports B2 and B3 at the as-of time on outcomes alone, with
B3's paired difference against B2, calibrations and subsets.

Intervals are weekly block bootstrap (hard rule 7).
"""

from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from nhl_edge.backtest import b2_report
from nhl_edge.backtest.metrics import DRAWS, LEVEL, SEED, bootstrap, week_of
from nhl_edge.backtest.subsets import SUBSETS
from nhl_edge.game.b2 import B2Model
from nhl_edge.game.b3 import INPUTS, B3Model
from nhl_edge.lake.schemas import PP_UNIT

MODEL = "B3"
REFERENCE = "B2"
BASELINE = "B1"
GAP = b2_report.GAP
GAPS_FILE = "gaps_b3.csv"
HOCKEY = "hockey"


def fit_rows(fits: dict[int, B3Model]) -> dict[str, Any]:
    return {
        str(season): {
            "l2": model.settings.l2,
            "intercept": model.intercept,
            "weights": dict(zip(INPUTS, model.weights, strict=True)),
            "games": model.games,
            "train_cutoff": model.train_cutoff.isoformat(),
        }
        for season, model in sorted(fits.items())
    }


def against(rows: pl.DataFrame, reference: str = REFERENCE) -> pl.DataFrame:
    """Per game, B3's log loss less the reference model's, on the games both scored."""
    keys = ["season", "game_id", "game_date"]
    return (
        rows.filter(pl.col("model") == MODEL)
        .select(*keys, "log_loss")
        .join(
            rows.filter(pl.col("model") == reference).select("game_id", other="log_loss"),
            on="game_id",
        )
        .with_columns(difference=pl.col("log_loss") - pl.col("other"))
    )


def _estimate(frame: pl.DataFrame, value: str) -> dict[str, Any]:
    """bootstrap's estimate, without an interval (low and high null) when a season's games fall
    in a single week: resampling one week cannot measure the spread (hard rule 7)."""
    found: dict[str, Any] = bootstrap(frame, value).to_dict()
    weeks = frame.group_by("season").agg(weeks=week_of(pl.col("game_date")).n_unique())
    if weeks["weeks"].min() < 2:  # type: ignore[operator]
        found |= {"low": None, "high": None}
    return found


def _estimates(frame: pl.DataFrame, value: str) -> dict[str, Any]:
    if frame.is_empty():
        return {"pooled": None, "per_season": {}}
    return {
        "pooled": _estimate(frame, value),
        "per_season": {
            str(season): _estimate(by_season, value)
            for (season,), by_season in frame.sort("season").group_by("season", maintain_order=True)
        },
    }


def subsets(rows: pl.DataFrame, flags: pl.DataFrame) -> dict[str, Any]:
    """B3 less B2 on each of gate 2's subsets and their union (any), with the games in each."""
    paired = against(rows).join(flags, on="game_id", how="inner")
    return {
        name: {"games": paired.filter(pl.col(name)).height}
        | _estimates(paired.filter(pl.col(name)), "difference")
        for name in (*SUBSETS, "any")
    }


def gap_rows(rows: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """The games where B3's probability differs from B1's by more than GAP, without results."""
    keys = ["season", "game_id", "game_date"]
    both = (
        rows.filter(pl.col("model") == MODEL)
        .select(*keys, p_b3="p_home")
        .join(
            rows.filter(pl.col("model") == BASELINE).select("game_id", p_b1="p_home"),
            on="game_id",
        )
        .with_columns(gap=pl.col("p_b3") - pl.col("p_b1"))
    )
    return (
        both.filter(pl.col("gap").abs() > GAP)
        .join(games.select("game_id", "home", "away"), on="game_id")
        .select("season", "game_id", "game_date", "home", "away", "p_b3", "p_b1", "gap")
        .sort("game_date", "game_id")
    )


def gaps(rows: pl.DataFrame, games: pl.DataFrame) -> dict[str, Any]:
    compared = rows.filter(pl.col("model") == MODEL).join(
        rows.filter(pl.col("model") == BASELINE).select("game_id"), on="game_id"
    )
    flagged = gap_rows(rows, games)
    counts = dict(flagged.group_by("season").len().iter_rows())
    return {
        "threshold": GAP,
        "games_compared": compared.height,
        "count": flagged.height,
        "per_season": {str(s): counts.get(s, 0) for s in sorted(compared["season"].unique())},
        "listed_in": GAPS_FILE,
    }


def write_gaps(predictions: pl.DataFrame, games: pl.DataFrame, out: Path) -> Path:
    """Every experiment's B3 gaps above GAP to out/gaps_b3.csv, for manual review (hard rule
    8)."""
    frames = [
        gap_rows(predictions.filter(pl.col("experiment") == experiment), games).select(
            pl.lit(experiment).alias("experiment"), pl.all()
        )
        for experiment in sorted(predictions["experiment"].unique().to_list())
    ]
    path = out / GAPS_FILE
    pl.concat(frames).with_columns(pl.col("p_b3", "p_b1", "gap").round(4)).write_csv(path)
    return path


def ice_time(lineups: pl.DataFrame, minutes: pl.DataFrame, scored: pl.Series) -> dict[str, Any]:
    """The projection's quality on the scored games' team-games with stints: the mean absolute
    error of a dressed candidate's minutes if he dresses (exp_5v5 over p_available) against his
    actual 5v5 minutes, and the share of the actual top five by power-play minutes that the
    projected unit named (ADR 0018). minutes is lineup.minutes.player_minutes."""
    keys = ["game_id", "team", "player_id"]
    wanted = pl.col("game_id").is_in(scored.implode())
    projected = lineups.filter(wanted, pl.col("role").is_in(["F", "D"]))
    actual = minutes.filter(wanted)
    mae = (
        projected.filter(pl.col("p_available") > 0)
        .join(actual.select(*keys, "season", "game_date", actual="5v5"), on=keys)
        .group_by("game_id", "team")
        .agg(
            pl.col("season", "game_date").first(),
            mae=(pl.col("exp_5v5") / pl.col("p_available") - pl.col("actual")).abs().mean(),
        )
    )
    ranked = actual.filter(pl.col("pp") > 0).with_columns(
        rank=pl.struct(pl.col("pp").neg(), "player_id").rank("ordinal").over("game_id", "team")
    )
    tops = (
        ranked.filter(pl.col("rank") <= PP_UNIT)
        .group_by("game_id", "team")
        .agg(pl.col("season", "game_date").first(), top=pl.col("player_id"))
        .filter(pl.col("top").list.len() == PP_UNIT)
    )
    named = (
        projected.filter(pl.col("pp_unit"))
        .group_by("game_id", "team")
        .agg(named=pl.col("player_id"))
    )
    units = tops.join(named, on=["game_id", "team"], how="inner").with_columns(
        pp_unit=pl.col("top").list.set_intersection(pl.col("named")).list.len() / PP_UNIT
    )
    return {
        "minutes_5v5_mae": _estimates(mae, "mae"),
        "pp_unit_accuracy": _estimates(units, "pp_unit"),
    }


def add(
    report: dict[str, Any],
    predictions: pl.DataFrame,
    fits: dict[str, dict[int, B3Model]],
    games: pl.DataFrame,
    goalie_starts: pl.DataFrame,
    boxscores: pl.DataFrame,
    lineups: pl.DataFrame,
    minutes: pl.DataFrame,
    flags: pl.DataFrame,
) -> dict[str, Any]:
    """The report with B3's fits, paired difference against B2, subsets, calibration, gaps and
    lineup quality under each experiment's B3, and each season's train_cutoff raised to B3's
    fits where they read a later row."""
    for experiment, body in report["experiments"].items():
        results = body["models"].get(MODEL)
        if results is None:
            continue
        rows = predictions.filter(pl.col("experiment") == experiment)
        scored = rows.filter(pl.col("model") == MODEL)
        results["fits"] = fit_rows(fits.get(experiment, {}))
        results[f"paired_against_{REFERENCE}"] = _estimates(against(rows), "difference")
        results["gate_2_subsets"] = subsets(rows, flags)
        results["calibration"] = b2_report.calibrated(scored)
        results["gaps_over_8_points"] = gaps(rows, games)
        results["lineup_quality"] = {
            "goalie_starts": b2_report.lineup_quality(goalie_starts, boxscores, scored["game_id"]),
            "ice_time": ice_time(lineups, minutes, scored["game_id"]),
        }
    for season, cutoff in report["train_cutoff"].items():
        later = [
            model.train_cutoff.isoformat()
            for by_season in fits.values()
            for fitted, model in by_season.items()
            if str(fitted) == season
        ]
        report["train_cutoff"][season] = max([cutoff, *later])
    return report


def hockey(
    predictions: pl.DataFrame,
    coverage: dict[str, dict[int, dict[str, int]]],
    b3_fits: dict[str, dict[int, B3Model]],
    flags: pl.DataFrame,
    seasons: list[int],
    run_version: str,
    now: datetime,
    b2_fits: dict[str, dict[int, B2Model]] | None = None,
) -> dict[str, Any]:
    """The hockey-only mode's report: each model's log loss, fits and calibration, and B3
    against B2 overall and on the subsets."""
    models = {}
    for (model,), rows in predictions.sort("model").group_by("model", maintain_order=True):
        models[str(model)] = {
            "log_loss": _estimates(rows, "log_loss"),
            "calibration": b2_report.calibrated(rows),
        }
    if b2_fits and REFERENCE in models:
        models[REFERENCE]["fits"] = b2_report.fit_rows(next(iter(b2_fits.values()), {}))
    if MODEL in models:
        models[MODEL]["fits"] = fit_rows(next(iter(b3_fits.values()), {}))
        models[MODEL][f"paired_against_{REFERENCE}"] = _estimates(
            against(predictions), "difference"
        )
        models[MODEL]["gate_2_subsets"] = subsets(predictions, flags)
    return {
        "version": run_version,
        "run_utc": now.isoformat(),
        "seasons": seasons,
        "mode": "hockey-only, at the as-of time, on outcomes alone (ADR 0023)",
        "bootstrap": {
            "block": "week (Monday, ET game date)",
            "draws": DRAWS,
            "seed": SEED,
            "level": LEVEL,
        },
        "coverage": {
            str(season): counts for season, counts in next(iter(coverage.values())).items()
        },
        "models": models,
    }


def hockey_file(report: dict[str, Any]) -> str:
    """The hockey-only report's file name, one per run: hockey-<version>-<HHMMSS>.json, the
    run's UTC time telling apart runs of one commit on one day."""
    at = datetime.fromisoformat(report["run_utc"]).strftime("%H%M%S")
    return f"{report['version']}-{at}.json".replace("backtest-hockey-", f"{HOCKEY}-", 1)


def hockey_runs(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The hockey-only report's rows for runs.csv (reports.RUNS_FIELDS): each model's pooled log
    loss, so every run on a season is logged (plan section 7)."""
    rows = []
    for model, body in report["models"].items():
        pooled = body["log_loss"]["pooled"]
        bounds = {k: "" if pooled[k] is None else f"{pooled[k]:.5f}" for k in ("low", "high")}
        rows.append(
            {
                "run_utc": report["run_utc"],
                "version": report["version"],
                "seasons": " ".join(map(str, report["seasons"])),
                "experiment": HOCKEY,
                "model": model,
                "method": "none",
                "games": pooled["games"],
                "log_loss": f"{pooled['mean']:.5f}",
                **bounds,
                "train_cutoff": " ".join(
                    fit["train_cutoff"] for fit in body.get("fits", {}).values()
                ),
            }
        )
    return rows
