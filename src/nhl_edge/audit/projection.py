"""The lineup model's report (#99, ADR 0017).

Per season, over every team-game with a boxscore:
- the Brier score over the team's skaters: the sum over its candidates of (p_available -
  dressed)², plus 1 for each dressed skater who was not a candidate (a newcomer, counted at
  probability 0);
- newcomers: the share of dressed skaters who were not candidates.

The Brier score has a weekly block bootstrap interval (hard rule 7), beside a reference that needs
no fit: he dresses if he dressed last game (probability 1 or 0). The model minus the reference is
paired by team-game. A held-out season shows only how many team-games were scored; the
development seasons count as held out until gate 2.

The fits come last, one per season, with their train_cutoff, and their team-games, coefficients
and expected newcomers only when every season they were fitted on is shown.
"""

from collections.abc import Collection, Sequence
from typing import Any

import polars as pl

from nhl_edge.backtest.metrics import Estimate, bootstrap
from nhl_edge.lineup.projection import FEATURES, AvailabilityModel, team_skater_games

REFERENCE = "dressed_last"


def team_game_scores(scored: pl.DataFrame, lineups: pl.DataFrame) -> pl.DataFrame:
    """One row per team-game of the scored seasons with a boxscore: its dressed skaters and
    newcomers, and the model's and the reference's Brier score."""
    seasons = scored["season"].unique().implode()
    team_games = (
        team_skater_games(lineups)
        .filter(pl.col("season").is_in(seasons))
        .select("game_id", "season", "game_date", "team", skaters=pl.col("players").list.len())
    )
    dressed = pl.col("dressed").cast(pl.Float64)
    per = (
        scored.filter(pl.col("dressed").is_not_null())
        .group_by("game_id", "team")
        .agg(
            hits=dressed.sum(),
            brier=((pl.col("p_available") - dressed) ** 2).sum(),
            brier_reference=((pl.col(REFERENCE) - dressed) ** 2).sum(),
        )
    )
    return (
        team_games.join(per, on=["game_id", "team"], how="left")
        .with_columns(pl.col("hits", "brier", "brier_reference").fill_null(0.0))
        .with_columns(newcomers=pl.col("skaters") - pl.col("hits"))
        .with_columns(
            brier=pl.col("brier") + pl.col("newcomers"),
            brier_reference=pl.col("brier_reference") + pl.col("newcomers"),
        )
        .with_columns(brier_minus_reference=pl.col("brier") - pl.col("brier_reference"))
        .sort("game_id", "team")
    )


def season_report(scores: pl.DataFrame, shown: Collection[int]) -> list[dict[str, Any]]:
    """One row per season: team-games scored, and for a shown season its figures."""
    rows = []
    for (season,), frame in scores.sort("season").group_by("season", maintain_order=True):
        row: dict[str, Any] = {"season": season, "team_games": frame.height}
        if season in shown:
            row |= {
                "newcomers": frame.select(
                    pl.col("newcomers").sum() / pl.col("skaters").sum()
                ).item(),
                "brier": bootstrap(frame, "brier"),
                "brier_reference": float(frame["brier_reference"].mean()),  # type: ignore[arg-type]
                "brier_minus_reference": bootstrap(frame, "brier_minus_reference"),
            }
        rows.append(row)
    return rows


def fits(models: Sequence[AvailabilityModel], shown: Collection[int]) -> list[dict[str, Any]]:
    """Each fit, with its team-games, coefficients and expected newcomers only when every season
    it read is shown."""
    rows = []
    for model in models:
        whole = set(model.seasons) <= set(shown)
        names = ("intercept", *FEATURES)
        rows.append(
            {
                "season": model.season,
                "trained_on": f"{model.seasons[0]} to {model.seasons[-1]}",
                "team_games": model.team_games if whole else None,
                "coefficients": dict(zip(names, model.coefficients, strict=True))
                if whole
                else None,
                "newcomers": model.newcomers if whole else None,
                "train_cutoff": model.train_cutoff.isoformat(),
            }
        )
    return rows


def _estimate(estimate: Estimate, signed: bool = False) -> str:
    sign = "+" if signed else ""
    return f"{estimate.mean:{sign}.4f} [{estimate.low:{sign}.4f}, {estimate.high:{sign}.4f}]"


def markdown_report(
    scores: pl.DataFrame,
    models: Sequence[AvailabilityModel],
    shown: Collection[int],
    version: str,
) -> str:
    lines = [
        f"# Lineup availability: {version}",
        "",
        "Each team-game's Brier score over its skaters: the sum over the candidates of",
        "(p_available - dressed)², plus 1 for each dressed skater who was not a candidate (a",
        "newcomer). The reference says a skater dresses if he dressed in the team's last game.",
        "Newcomers is the share of dressed skaters who were not candidates. Intervals are 95%",
        "weekly block bootstrap; the difference is paired by team-game. Held-out seasons, the",
        "development seasons among them until gate 2, show only how many team-games were scored.",
        "",
        "## Per season",
        "",
        "| Season | Team-games | Newcomers | Brier | Reference | Minus the reference |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in season_report(scores, shown):
        if "brier" in row:
            cells = (
                f"{row['newcomers']:.2%} | {_estimate(row['brier'])} | "
                f"{row['brier_reference']:.4f} | {_estimate(row['brier_minus_reference'], True)}"
            )
        else:
            cells = "held out | | | "
        lines.append(f"| {row['season']} | {row['team_games']:,} | {cells} |")
    names = ("intercept", *FEATURES)
    lines += [
        "",
        "## Fits",
        "",
        "Expected newcomers per team-game, in a team's other games and in its first of a season,",
        "set each team-game's total: 18 less them.",
        "",
        f"| Season scored | Trained on | Team-games | Newcomers (other, first) | "
        f"{' | '.join(names)} | train_cutoff |",
        "| --- | --- | ---: | ---: | " + "---: | " * len(names) + "--- |",
    ]
    for row in fits(models, shown):
        if row["coefficients"] is None:
            cells = ["held out", "", *[""] * len(names)]
        else:
            other, first = row["newcomers"]
            cells = [
                f"{row['team_games']:,}",
                f"{other:.3f}, {first:.3f}",
                *(f"{value:+.3f}" for value in row["coefficients"].values()),
            ]
        cells = [str(row["season"]), row["trained_on"], *cells, row["train_cutoff"]]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
