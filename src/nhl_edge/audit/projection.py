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

The projected ice time (#100, ADR 0018) is scored per season over the team-games with stints,
against "last game's minutes":
- the 5v5 minutes MAE over the dressed candidates with an earlier game for the team: his minutes
  if he dresses against his actual 5v5 minutes, beside his minutes in his latest earlier game;
- the power-play unit accuracy: the share of the actual top five by power-play minutes that the
  projection named, beside the team's top five in its latest earlier game, over the team-games
  where five skaters had power-play time.
Both have weekly block bootstrap intervals, the differences paired by team-game. The figures each
season's minutes take from the season before show only when that season is shown.
"""

from collections.abc import Collection, Sequence
from typing import Any

import polars as pl

from nhl_edge.backtest.metrics import Estimate, bootstrap
from nhl_edge.lake.schemas import PP_UNIT
from nhl_edge.lineup.minutes import STATES, SeasonConstants
from nhl_edge.lineup.projection import FEATURES, SKATER_ROLES, AvailabilityModel, team_skater_games

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


def _power_play_tops(minutes: pl.DataFrame) -> pl.DataFrame:
    """Each team-game's top five by power-play minutes (ties to the lower player id), where five
    skaters had power-play time."""
    ranked = minutes.filter(pl.col("pp") > 0).with_columns(
        rank=pl.struct(pl.col("pp").neg(), "player_id").rank("ordinal").over("game_id", "team")
    )
    return (
        ranked.filter(pl.col("rank") <= PP_UNIT)
        .group_by("game_id", "team", "line")
        .agg(top=pl.col("player_id"), game_utc=pl.col("observed_utc").first())
        .filter(pl.col("top").list.len() == PP_UNIT)
    )


def ice_time_scores(projected: pl.DataFrame, minutes: pl.DataFrame) -> pl.DataFrame:
    """One row per team-game of the projected seasons with stints: its 5v5 minutes MAE and the
    reference's (null without a dressed candidate with an earlier game), and its power-play unit
    accuracy and the reference's (null without five skaters on the power play, or for the
    reference without such an earlier game)."""
    keys = ["game_id", "team", "player_id"]
    actual = minutes.select(*keys, actual_5v5="5v5")
    mae = (
        projected.join(actual, on=keys, how="inner")
        .filter(pl.col("last_5v5").is_not_null())
        .group_by("game_id", "team")
        .agg(
            mae=(pl.col("min_5v5") - pl.col("actual_5v5")).abs().mean(),
            mae_reference=(pl.col("last_5v5") - pl.col("actual_5v5")).abs().mean(),
        )
    )
    tops = _power_play_tops(minutes)
    targets = projected.select(
        "game_id", "season", "game_date", "team", "line", "as_of_utc"
    ).unique(subset=["game_id", "team"])
    played = minutes.select("game_id", "team").unique()
    named = (
        projected.filter(pl.col("pp_unit"))
        .group_by("game_id", "team")
        .agg(named=pl.col("player_id"))
    )
    earlier = (
        targets.sort("as_of_utc")
        .join_asof(
            tops.select("line", "game_utc", reference=pl.col("top")).sort("game_utc"),
            left_on="as_of_utc",
            right_on="game_utc",
            by="line",
            strategy="backward",
            allow_exact_matches=False,
            check_sortedness=False,  # both sides are sorted by time above
        )
        .select("game_id", "team", "reference")
    )
    share = pl.col("top").list.set_intersection
    return (
        targets.join(played, on=["game_id", "team"], how="semi")
        .join(mae, on=["game_id", "team"], how="left")
        .join(tops.select("game_id", "team", "top"), on=["game_id", "team"], how="left")
        .join(named, on=["game_id", "team"], how="left")
        .join(earlier, on=["game_id", "team"], how="left")
        .with_columns(
            pp_unit=share(pl.col("named")).list.len() / PP_UNIT,
            pp_unit_reference=share(pl.col("reference")).list.len() / PP_UNIT,
        )
        .with_columns(
            mae_minus_reference=pl.col("mae") - pl.col("mae_reference"),
            pp_unit_minus_reference=pl.col("pp_unit") - pl.col("pp_unit_reference"),
        )
        .select(
            "game_id",
            "season",
            "game_date",
            "team",
            "mae",
            "mae_reference",
            "mae_minus_reference",
            "pp_unit",
            "pp_unit_reference",
            "pp_unit_minus_reference",
        )
        .sort("game_id", "team")
    )


def ice_time_report(scores: pl.DataFrame, shown: Collection[int]) -> list[dict[str, Any]]:
    """One row per season: team-games with stints, and for a shown season its figures, each over
    the team-games where it and its reference are defined."""
    rows = []
    for (season,), frame in scores.sort("season").group_by("season", maintain_order=True):
        row: dict[str, Any] = {"season": season, "team_games": frame.height}
        if season in shown:
            for name in ("mae", "pp_unit"):
                paired = frame.filter(
                    pl.col(name).is_not_null(), pl.col(f"{name}_reference").is_not_null()
                )
                row |= {
                    name: bootstrap(paired, name),
                    f"{name}_reference": float(paired[f"{name}_reference"].mean()),  # type: ignore[arg-type]
                    f"{name}_minus_reference": bootstrap(paired, f"{name}_minus_reference"),
                }
        rows.append(row)
    return rows


def _estimate(estimate: Estimate, signed: bool = False) -> str:
    sign = "+" if signed else ""
    return f"{estimate.mean:{sign}.4f} [{estimate.low:{sign}.4f}, {estimate.high:{sign}.4f}]"


def markdown_report(
    scores: pl.DataFrame,
    models: Sequence[AvailabilityModel],
    shown: Collection[int],
    version: str,
    ice_time: pl.DataFrame | None = None,
    constants: Sequence[SeasonConstants] = (),
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
    if ice_time is not None:
        lines += _ice_time_lines(ice_time, constants, shown)
    return "\n".join(lines) + "\n"


def _ice_time_lines(
    scores: pl.DataFrame, constants: Sequence[SeasonConstants], shown: Collection[int]
) -> list[str]:
    lines = [
        "",
        "## Ice time",
        "",
        "Over the team-games with stints (#100, ADR 0018). 5v5 MAE: minutes per dressed candidate",
        "between his projected minutes if he dresses and his actual 5v5 minutes; the reference is",
        "his 5v5 minutes in his latest earlier game for the team. PP unit: the share of the actual",
        "top five by power-play minutes the projection named; the reference is the team's top",
        "five in its latest earlier game. Lower MAE and higher PP unit accuracy are better.",
        "",
        "| Season | Team-games | 5v5 MAE | Reference | Minus the reference | PP unit "
        "| Reference | Minus the reference |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in ice_time_report(scores, shown):
        if "mae" in row:
            cells = (
                f"{_estimate(row['mae'])} | {row['mae_reference']:.4f} | "
                f"{_estimate(row['mae_minus_reference'], True)} | {_estimate(row['pp_unit'])} | "
                f"{row['pp_unit_reference']:.4f} | "
                f"{_estimate(row['pp_unit_minus_reference'], True)}"
            )
        else:
            cells = "held out | | | | | "
        lines.append(f"| {row['season']} | {row['team_games']:,} | {cells} |")
    lines += [
        "",
        "## Ice-time figures from the season before",
        "",
        "Per role (F, D) and state: skater-minutes per team-game (the total the candidates and",
        "replacements add up to), and the pull in games toward the role's average minutes.",
        "",
        "| Season | From | "
        + " | ".join(
            f"{role} {state} total | {role} {state} pull"
            for role in SKATER_ROLES
            for state in STATES
        )
        + " |",
        "| --- | --- | " + "---: | " * (2 * len(SKATER_ROLES) * len(STATES)),
    ]
    for c in constants:
        if c.source in shown:
            cells = [
                f"{c.total[role, state]:.2f} | {c.pull[role, state]:.2f}"
                for role in SKATER_ROLES
                for state in STATES
            ]
        else:
            cells = ["held out"] + [""] * (2 * len(SKATER_ROLES) * len(STATES) - 1)
        lines.append(f"| {c.season} | {c.source} | " + " | ".join(cells) + " |")
    return lines
