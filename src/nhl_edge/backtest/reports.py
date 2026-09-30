"""Backtest reports: reports/backtest/summary.json for the run, and one line per pooled estimate
appended to reports/backtest/runs.csv, the run history the model card points to.

Every estimate carries its weekly block bootstrap interval (hard rule 7). Every other model is
compared per game with B1, the recalibrated market (hard rule 3). The de-vig methods are paired
against the multiplicative method, as evidence for the ADR that chooses the default (#10), and E2
is paired against E1 on the games both scored. Each B1 fit is reported with its train_cutoff.
"""

import csv
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from nhl_edge.backtest.metrics import DRAWS, LEVEL, SEED, Estimate, bootstrap
from nhl_edge.backtest.walk_forward import Coverage, Fits
from nhl_edge.market.devig import Method
from nhl_edge.market.recalibration import Recalibration

REFERENCE_METHOD = Method.MULTIPLICATIVE
# The market baseline every other model is compared with (hard rule 3).
BASELINE = "B1"
# What each experiment's market price is, and the assumption it rests on (ADR 0006).
MARKETS = {
    "E1": "SBR closing moneyline, read at the start: the information test, not a tradable price",
    "E2": "SBR opening moneyline, assumed available from 10:00 US Eastern on the game date and "
    "read one second later (ADR 0006). If an opener was posted earlier and had moved by then, "
    "E2's result is an upper bound; coverage counts the games whose opener differs from the close. "
    "E2 refuses an opener whose de-vigged home probability is outside 0.15 to 0.85 (ADR 0007)",
}
RUNS_FIELDS = [
    "run_utc",
    "version",
    "seasons",
    "experiment",
    "model",
    "method",
    "games",
    "log_loss",
    "low",
    "high",
    "train_cutoff",
]


# The files whose uncommitted changes would make a run's metrics differ from its commit's.
CODE_PATHS = ["src", "pyproject.toml", "uv.lock"]


def _git(args: list[str], cwd: Path | None) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True, cwd=cwd
    ).stdout.strip()


def version(component: str, now: datetime, cwd: Path | None = None) -> str:
    """The artifact version <component>-<yyyymmdd>-<shortsha> (CLAUDE.md conventions), with
    -dirty appended when the code has uncommitted changes, so the commit alone does not
    reproduce the run."""
    try:
        sha = _git(["rev-parse", "--short", "HEAD"], cwd)
        if _git(["status", "--porcelain", "--", *CODE_PATHS], cwd):
            sha += "-dirty"
    except (OSError, subprocess.CalledProcessError):
        sha = "nogit"
    return f"{component}-{now:%Y%m%d}-{sha}"


def _estimates(frame: pl.DataFrame, value: str) -> dict[str, Any]:
    per_season = {
        str(season): bootstrap(rows, value).to_dict()
        for (season,), rows in frame.sort("season").group_by("season", maintain_order=True)
    }
    return {"pooled": bootstrap(frame, value).to_dict(), "per_season": per_season}


def paired(predictions: pl.DataFrame, method: Method) -> pl.DataFrame:
    """Per game, the method's log loss minus the reference method's, on the games both scored."""
    keys = ["experiment", "model", "season", "game_id", "game_date"]
    reference = predictions.filter(pl.col("method") == REFERENCE_METHOD.value).select(
        *keys, reference=pl.col("log_loss")
    )
    return (
        predictions.filter(pl.col("method") == method.value)
        .join(reference, on=keys)
        .with_columns(difference=pl.col("log_loss") - pl.col("reference"))
    )


def against_baseline(predictions: pl.DataFrame) -> pl.DataFrame:
    """Per game, each other model's log loss minus B1's in the same experiment, on the games both
    scored."""
    keys = ["experiment", "season", "game_id", "game_date"]
    baseline = predictions.filter(pl.col("model") == BASELINE).select(
        *keys, baseline=pl.col("log_loss")
    )
    return (
        predictions.filter(pl.col("model") != BASELINE)
        .join(baseline, on=keys)
        .with_columns(difference=pl.col("log_loss") - pl.col("baseline"))
    )


def e2_against_e1(predictions: pl.DataFrame) -> pl.DataFrame:
    """Per game, E2's log loss minus E1's for the same model and method, on the games both
    scored: how much predicting at the opener costs against the close."""
    keys = ["model", "method", "season", "game_id", "game_date"]
    e1 = predictions.filter(pl.col("experiment") == "E1").select(*keys, e1=pl.col("log_loss"))
    return (
        predictions.filter(pl.col("experiment") == "E2")
        .join(e1, on=keys)
        .with_columns(difference=pl.col("log_loss") - pl.col("e1"))
    )


def _fit(fit: Recalibration) -> dict[str, Any]:
    return {
        "intercept": fit.intercept,
        "slope": fit.slope,
        "games": fit.games,
        "train_cutoff": fit.train_cutoff.isoformat(),
    }


def experiment(
    experiment: str, predictions: pl.DataFrame, coverage: Coverage, fits: Fits
) -> dict[str, Any]:
    """One experiment's market, coverage per season, and per model its log losses, its paired
    differences against the multiplicative method and against B1, or B1's fits."""
    rows = predictions.filter(pl.col("experiment") == experiment).sort("model")
    compared = against_baseline(rows)
    models: dict[str, Any] = {}
    for (model,), by_model in rows.group_by("model", maintain_order=True):
        methods = {
            str(method): _estimates(by_method, "log_loss")
            for (method,), by_method in by_model.sort("method").group_by(
                "method", maintain_order=True
            )
        }
        results: dict[str, Any] = {"log_loss": methods}
        against = {
            method.value: _estimates(paired(by_model, method), "difference")
            for method in Method
            if method is not REFERENCE_METHOD
            and by_model.filter(pl.col("method") == method.value).height
        }
        if against:
            results[f"paired_against_{REFERENCE_METHOD.value}"] = against
        if model == BASELINE:
            results["fits"] = {str(season): _fit(fit) for season, fit in fits[experiment].items()}
        else:
            versus = compared.filter(pl.col("model") == model)
            results[f"paired_against_{BASELINE}"] = {
                str(method): _estimates(by_method, "difference")
                for (method,), by_method in versus.sort("method").group_by(
                    "method", maintain_order=True
                )
            }
        models[str(model)] = results
    return {
        "market": MARKETS[experiment],
        "coverage": {str(s): counts for s, counts in coverage[experiment].items()},
        "models": models,
    }


def against_e1(predictions: pl.DataFrame) -> dict[str, Any]:
    """E2 against E1 per model and method, pooled and per season."""
    later = e2_against_e1(predictions)
    return {
        str(model): {
            str(method): _estimates(by_method, "difference")
            for (method,), by_method in by_model.group_by("method", maintain_order=True)
        }
        for (model,), by_model in later.sort("model", "method").group_by(
            "model", maintain_order=True
        )
    }


def summary(
    predictions: pl.DataFrame,
    coverage: Coverage,
    fits: Fits,
    seasons: list[int],
    run_version: str,
    now: datetime,
) -> dict[str, Any]:
    return {
        "version": run_version,
        "run_utc": now.isoformat(),
        "seasons": seasons,
        # Each season's fold: the last result any of its fits read, before the season starts.
        "train_cutoff": {
            str(season): max(fits[e][season].train_cutoff for e in fits).isoformat()
            for season in seasons
        },
        "bootstrap": {
            "block": "week (Monday, ET game date)",
            "draws": DRAWS,
            "seed": SEED,
            "level": LEVEL,
        },
        # From coverage, so an experiment with no scored game still reports why.
        "experiments": {
            str(name): experiment(str(name), predictions, coverage, fits)
            for name in sorted(coverage)
        },
        "e2_against_e1": against_e1(predictions),
    }


def write(report: dict[str, Any], out: Path) -> Path:
    """Write summary.json and append the pooled log losses to runs.csv. When runs.csv's columns
    change, its earlier rows are carried over onto the new columns, and the file is replaced only
    once the new one is written."""
    out.mkdir(parents=True, exist_ok=True)
    path = out / "summary.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    rows = []
    for experiment, body in report["experiments"].items():
        for model, results in body["models"].items():
            cutoffs = " ".join(f["train_cutoff"] for f in results.get("fits", {}).values())
            for method, estimates in results["log_loss"].items():
                pooled = Estimate(**estimates["pooled"])
                rows.append(
                    {
                        "run_utc": report["run_utc"],
                        "version": report["version"],
                        "seasons": " ".join(map(str, report["seasons"])),
                        "experiment": experiment,
                        "model": model,
                        "method": method,
                        "games": pooled.games,
                        "log_loss": f"{pooled.mean:.5f}",
                        "low": f"{pooled.low:.5f}",
                        "high": f"{pooled.high:.5f}",
                        "train_cutoff": cutoffs,
                    }
                )
    runs = out / "runs.csv"
    stale = False
    if runs.exists():
        with runs.open(newline="") as handle:
            reader = csv.DictReader(handle)
            stale = reader.fieldnames != RUNS_FIELDS
            if stale:
                # A column that is gone is dropped and a new one left empty.
                rows = [{f: row.get(f) or "" for f in RUNS_FIELDS} for row in reader] + rows
    if runs.exists() and not stale:
        with runs.open("a", newline="") as handle:
            csv.DictWriter(handle, fieldnames=RUNS_FIELDS).writerows(rows)
        return path
    rewritten = runs.with_suffix(".csv.tmp")
    with rewritten.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RUNS_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    rewritten.replace(runs)
    return path
