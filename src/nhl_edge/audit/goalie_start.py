"""The goalie-start model's report (#76, ADR 0012).

Per season, over every team-game with a flagged starter:
- the Brier score: over the team's goalies, the sum of (p_start - started)², with a starter
  outside the candidates counted at p_start = 0, so it runs from 0 (certain and right) to 2;
- top-pick accuracy: how often the starter had the highest probability, a tie sharing the credit;
- missed starters: the share of team-games whose starter was not a candidate.

The Brier score and top-pick accuracy have weekly block bootstrap intervals (hard rule 7), beside
a reference that needs no fit: each candidate's share of the team's starts in its last ten games.
The model minus the reference is paired by team-game. A held-out season shows only how many
team-games were scored; the development seasons count as held out until gate 1.

The fits come last, one per season, with their train_cutoff, and their team-games and
coefficients only when every season they were fitted on is shown.
"""

from collections.abc import Collection, Sequence
from typing import Any

import polars as pl

from nhl_edge.backtest.metrics import Estimate, bootstrap
from nhl_edge.lineup.goalie_start import FEATURES, GoalieStartModel, team_goalie_games

REFERENCE = "recent_share"


def _top_credit(probability: str) -> pl.Expr:
    """1/k when the starter is among the k candidates sharing the highest probability, else 0."""
    best = pl.col(probability) == pl.col(probability).max()
    return pl.col("started").filter(best).sum() / best.sum()


def team_game_scores(scored: pl.DataFrame, lineups: pl.DataFrame) -> pl.DataFrame:
    """One row per team-game of the scored seasons with a flagged starter: whether he was a
    candidate, and the model's and the reference's Brier score and top-pick credit."""
    seasons = scored["season"].unique().implode()
    starters = (
        team_goalie_games(lineups)
        .filter(pl.col("season").is_in(seasons), pl.col("starter").is_not_null())
        .select("game_id", "season", "game_date", "team", "starter")
    )
    rows = scored.join(starters.select("game_id", "team", "starter"), on=["game_id", "team"])
    started = (pl.col("goalie_id") == pl.col("starter")).cast(pl.Float64)
    per = (
        rows.with_columns(started=started)
        .group_by("game_id", "team")
        .agg(
            hit=pl.col("started").sum(),
            brier=((pl.col("p_start") - pl.col("started")) ** 2).sum(),
            brier_reference=((pl.col(REFERENCE) - pl.col("started")) ** 2).sum(),
            top=_top_credit("p_start"),
            top_reference=_top_credit(REFERENCE),
        )
    )
    missed = 1 - pl.col("hit")
    return (
        starters.join(per, on=["game_id", "team"], how="left")
        .with_columns(pl.col("hit", "top", "top_reference").fill_null(0.0))
        .with_columns(
            missed=missed,
            brier=pl.col("brier").fill_null(0.0) + missed,
            brier_reference=pl.col("brier_reference").fill_null(0.0) + missed,
        )
        .with_columns(
            brier_minus_reference=pl.col("brier") - pl.col("brier_reference"),
            top_minus_reference=pl.col("top") - pl.col("top_reference"),
        )
        .sort("game_id", "team")
    )


def season_report(scores: pl.DataFrame, shown: Collection[int]) -> list[dict[str, Any]]:
    """One row per season: team-games scored, and for a shown season its figures."""
    rows = []
    for (season,), frame in scores.sort("season").group_by("season", maintain_order=True):
        row: dict[str, Any] = {"season": season, "team_games": frame.height}
        if season in shown:
            row |= {
                "missed": float(frame["missed"].mean()),  # type: ignore[arg-type]
                "brier": bootstrap(frame, "brier"),
                "brier_reference": float(frame["brier_reference"].mean()),  # type: ignore[arg-type]
                "brier_minus_reference": bootstrap(frame, "brier_minus_reference"),
                "top": bootstrap(frame, "top"),
                "top_reference": float(frame["top_reference"].mean()),  # type: ignore[arg-type]
                "top_minus_reference": bootstrap(frame, "top_minus_reference"),
            }
        rows.append(row)
    return rows


def fits(models: Sequence[GoalieStartModel], shown: Collection[int]) -> list[dict[str, Any]]:
    """Each fit, with its team-games and coefficients only when every season it read is shown."""
    rows = []
    for model in models:
        whole = set(model.seasons) <= set(shown)
        rows.append(
            {
                "season": model.season,
                "trained_on": f"{model.seasons[0]} to {model.seasons[-1]}",
                "team_games": model.team_games if whole else None,
                "coefficients": dict(zip(FEATURES, model.coefficients, strict=True))
                if whole
                else None,
                "train_cutoff": model.train_cutoff.isoformat(),
            }
        )
    return rows


def _estimate(estimate: Estimate, signed: bool = False) -> str:
    sign = "+" if signed else ""
    return f"{estimate.mean:{sign}.4f} [{estimate.low:{sign}.4f}, {estimate.high:{sign}.4f}]"


def markdown_report(
    scores: pl.DataFrame, models: Sequence[GoalieStartModel], shown: Collection[int], version: str
) -> str:
    lines = [
        f"# Goalie starts: {version}",
        "",
        "Each team-game's Brier score over its goalies (0 is certain and right, 2 certain and",
        "wrong; a starter who was not a candidate counts at probability 0), how often the",
        "starter was the top pick, and how often he was not a candidate. The reference gives",
        "each candidate his share of the team's starts in its last ten games. Intervals are 95%",
        "weekly block bootstrap; the differences are paired by team-game. Held-out seasons, the",
        "development seasons among them until gate 1, show only how many team-games were scored.",
        "",
        "## Per season",
        "",
        "| Season | Team-games | Missed starters | Brier | Reference | Minus the reference | "
        "Top pick | Reference | Minus the reference |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in season_report(scores, shown):
        if "brier" in row:
            cells = (
                f"{row['missed']:.2%} | {_estimate(row['brier'])} | "
                f"{row['brier_reference']:.4f} | {_estimate(row['brier_minus_reference'], True)} | "
                f"{_estimate(row['top'])} | {row['top_reference']:.4f} | "
                f"{_estimate(row['top_minus_reference'], True)}"
            )
        else:
            cells = "held out | | | | | | "
        lines.append(f"| {row['season']} | {row['team_games']:,} | {cells} |")
    names = " | ".join(FEATURES)
    lines += [
        "",
        "## Fits",
        "",
        f"| Season scored | Trained on | Team-games | {names} | train_cutoff |",
        "| --- | --- | ---: | " + "---: | " * len(FEATURES) + "--- |",
    ]
    for row in fits(models, shown):
        if row["coefficients"] is None:
            cells = ["held out", *[""] * len(FEATURES)]
        else:
            cells = [
                f"{row['team_games']:,}",
                *(f"{value:+.3f}" for value in row["coefficients"].values()),
            ]
        cells = [str(row["season"]), row["trained_on"], *cells, row["train_cutoff"]]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
