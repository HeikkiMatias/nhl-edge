"""B2's part of the backtest report (#78, ADR 0013), beside its log loss and its paired difference
against B1, which reports.experiment gives every model:
- its fits, one per experiment and season, with their weights and train_cutoff;
- its calibration intercept and slope: a logistic regression of the outcome on the log-odds of
  B2's probability, where 0 and 1 mean calibrated;
- how many games differ from B1 by more than GAP (hard rule 8), and the games themselves in
  gaps.csv for manual review, without their results, so the review looks at the inputs, not at
  who won;
- lineup quality: the goalie-start model's Brier score over the team-games of the games B2
  scored in the experiment.

Intervals are weekly block bootstrap (hard rule 7).
"""

from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from nhl_edge.backtest.metrics import DRAWS, SEED, bootstrap, calibration
from nhl_edge.game.b2 import INPUTS, B2Model
from nhl_edge.lineup.goalie_start import team_goalie_games

MODEL = "B2"
BASELINE = "B1"
GAP = 0.08
GAPS_FILE = "gaps.csv"


def fit_rows(fits: dict[int, B2Model]) -> dict[str, Any]:
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


def calibrated(rows: pl.DataFrame) -> dict[str, Any]:
    """Calibration intercept and slope of p_home against home_win, pooled and per season."""

    def estimate(frame: pl.DataFrame) -> dict[str, Any]:
        found = calibration(frame, "p_home", "home_win", DRAWS, SEED)
        return {
            name: interval.to_dict() | {"games": frame.height} for name, interval in found.items()
        }

    return {
        "pooled": estimate(rows),
        "per_season": {
            str(season): estimate(frame)
            for (season,), frame in rows.sort("season").group_by("season", maintain_order=True)
        },
    }


def gap_rows(rows: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """The games of one experiment where B2's probability differs from B1's by more than GAP,
    with both and the teams, but not the result."""
    keys = ["season", "game_id", "game_date"]
    both = (
        rows.filter(pl.col("model") == MODEL)
        .select(*keys, p_b2="p_home")
        .join(
            rows.filter(pl.col("model") == BASELINE).select("game_id", p_b1="p_home"),
            on="game_id",
        )
        .with_columns(gap=pl.col("p_b2") - pl.col("p_b1"))
    )
    return (
        both.filter(pl.col("gap").abs() > GAP)
        .join(games.select("game_id", "home", "away"), on="game_id")
        .select("season", "game_id", "game_date", "home", "away", "p_b2", "p_b1", "gap")
        .sort("game_date", "game_id")
    )


def gaps(rows: pl.DataFrame, games: pl.DataFrame) -> dict[str, Any]:
    """How many games B2 and B1 both scored, and how many differ by more than GAP, per season.
    The games themselves go to gaps.csv (write_gaps)."""
    compared = rows.filter(pl.col("model") == MODEL).join(
        rows.filter(pl.col("model") == BASELINE).select("game_id"), on="game_id"
    )
    flagged = gap_rows(rows, games)
    counts = dict(flagged.group_by("season").len().iter_rows())
    seasons = sorted(compared["season"].unique().to_list())
    return {
        "threshold": GAP,
        "games_compared": compared.height,
        "count": flagged.height,
        "per_season": {str(s): counts.get(s, 0) for s in seasons},
        "listed_in": GAPS_FILE,
    }


def write_gaps(predictions: pl.DataFrame, games: pl.DataFrame, out: Path) -> Path:
    """Every experiment's gaps above GAP to out/gaps.csv, for manual review (hard rule 8)."""
    frames = [
        gap_rows(predictions.filter(pl.col("experiment") == experiment), games).select(
            pl.lit(experiment).alias("experiment"), pl.all()
        )
        for experiment in sorted(predictions["experiment"].unique().to_list())
    ]
    path = out / GAPS_FILE
    pl.concat(frames).with_columns(pl.col("p_b2", "p_b1", "gap").round(4)).write_csv(path)
    return path


def lineup_quality(
    goalie_starts: pl.DataFrame, lineups: pl.DataFrame, scored: pl.Series
) -> dict[str, Any]:
    """The goalie-start model's Brier score per team-game with a flagged starter, over the scored
    games, a starter who was not a candidate counting at probability 0, and the share of such
    starters."""
    starters = (
        team_goalie_games(lineups)
        .filter(pl.col("game_id").is_in(scored.implode()), pl.col("starter").is_not_null())
        .select("game_id", "season", "game_date", "team", "starter")
    )
    rows = goalie_starts.join(starters.select("game_id", "team", "starter"), on=["game_id", "team"])
    started = (pl.col("goalie_id") == pl.col("starter")).cast(pl.Float64)
    per = rows.group_by("game_id", "team").agg(
        hit=started.sum(), brier=((pl.col("p_start") - started) ** 2).sum()
    )
    scores = (
        starters.join(per, on=["game_id", "team"], how="left")
        .with_columns(pl.col("hit", "brier").fill_null(0.0))
        .with_columns(missed=1 - pl.col("hit"), brier=pl.col("brier") + 1 - pl.col("hit"))
    )

    def estimate(frame: pl.DataFrame) -> dict[str, Any]:
        return {
            "brier": bootstrap(frame, "brier").to_dict(),
            "missed": float(np.mean(frame["missed"].to_numpy())),
        }

    return {
        "pooled": estimate(scores),
        "per_season": {
            str(season): estimate(frame)
            for (season,), frame in scores.sort("season").group_by("season", maintain_order=True)
        },
    }


def add(
    report: dict[str, Any],
    predictions: pl.DataFrame,
    fits: dict[str, dict[int, B2Model]],
    games: pl.DataFrame,
    goalie_starts: pl.DataFrame,
    lineups: pl.DataFrame,
) -> dict[str, Any]:
    """The report with B2's fits, calibration, gaps and lineup quality under each experiment's
    B2, the lineup quality over the games B2 scored there, and each season's train_cutoff raised
    to B2's fits where they read a later row than B1's (Codex on #90)."""
    for experiment, body in report["experiments"].items():
        results = body["models"].get(MODEL)
        if results is None:
            continue
        rows = predictions.filter(pl.col("experiment") == experiment)
        scored = rows.filter(pl.col("model") == MODEL)
        results["fits"] = fit_rows(fits.get(experiment, {}))
        results["calibration"] = calibrated(scored)
        results["gaps_over_8_points"] = gaps(rows, games)
        results["lineup_quality"] = {
            "goalie_starts": lineup_quality(goalie_starts, lineups, scored["game_id"])
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
