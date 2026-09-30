"""Whether SBR's change of closing book matters (#65): a diagnostic reported beside the backtest,
never an input to it.

From the first game of 2018-19, SBR's closes carry about 2.3% vig instead of 3.8%, while its
openers stay at about 4% (the #9 audit): the closes likely come from a sharper book from then on.
The owner kept the closes as they are (2026-09-30), and this diagnostic shows whether it matters.
It runs the walk-forward (walk_forward.run) on every open season that has an earlier one to fit
B1 on, so every B1 is fitted before its season starts, and every E2 opener is refused or kept as
in the backtest (ADR 0007). Then it compares the seasons before the switch with those from it on:
- B0's E2 minus E1. Before 2018-19 one book set both prices, so this is the cost of predicting at
  the opener alone. From 2018-19 on it also holds the change of book.
- B0 minus B1, per experiment: what B1, fitted on the seasons before, adds to the market. If a
  recalibration learnt on the old book's closes misfit the new book's, B1 would fall behind B0 from
  2018-19 on. This compares the whole recalibration, intercept and slope, out of sample.

An era comparison also holds whatever else changed between the seasons, such as the number of
seasons each B1 is fitted on. The diagnostic covers the open seasons the backtest reads: every one
up to its latest test season, so the default run, on the development seasons, covers 2011-12 to
2021-22 (2010-11 has no earlier season to fit B1 on).
"""

from typing import Any

import polars as pl

from nhl_edge.backtest import reports
from nhl_edge.backtest.metrics import DRAWS, bootstrap, difference
from nhl_edge.backtest.seasons import OPEN_SEASONS
from nhl_edge.backtest.walk_forward import B1_METHOD, run

# The first season whose closes come from the sharper book (the #9 audit).
SWITCH_SEASON = 20182019
SAME_BOOK, NEW_CLOSING_BOOK = "same_book", "new_closing_book"
DESCRIPTION = (
    "SBR's closes come from a lower-vig book from 2018-19 on, its openers from the same book "
    "throughout (#65). The walk-forward runs on every open season the backtest reads that has an "
    "earlier one to fit B1 on, so each B1 is fitted before its season starts and E2 refuses "
    "implausible openers as in ADR 0007. Per season and per era: B0's E2 minus E1, and B0 minus "
    "B1 in each experiment (positive when B1 beats the market). Era differences are the new "
    "closing book minus the same book; they also hold whatever else changed between the seasons, "
    "such as how many seasons each B1 is fitted on."
)


def era(season: pl.Expr) -> pl.Expr:
    return (
        pl.when(season < SWITCH_SEASON).then(pl.lit(SAME_BOOK)).otherwise(pl.lit(NEW_CLOSING_BOOK))
    )


def _by_era(frame: pl.DataFrame, draws: int) -> dict[str, Any]:
    """The per-game difference's mean per season and per era, with weekly block bootstrap
    intervals, and the difference between the eras when both are present."""
    frame = frame.with_columns(era=era(pl.col("season"))).sort("season", "game_id")
    eras = [name for name in (SAME_BOOK, NEW_CLOSING_BOOK) if name in set(frame["era"])]
    result: dict[str, Any] = {
        "per_season": {
            str(season): bootstrap(rows, "difference", draws).to_dict()
            for (season,), rows in frame.group_by("season", maintain_order=True)
        },
        "eras": {
            name: bootstrap(frame.filter(pl.col("era") == name), "difference", draws).to_dict()
            for name in eras
        },
    }
    if len(eras) == 2:
        result["era_difference"] = difference(
            frame.filter(pl.col("era") == NEW_CLOSING_BOOK),
            frame.filter(pl.col("era") == SAME_BOOK),
            "difference",
            draws,
        ).to_dict()
    return result


def seasons_of(sbr_odds: pl.DataFrame) -> list[int]:
    """The open seasons sbr_odds holds, but the first, which has no earlier season to fit B1 on."""
    held = sorted(s for s in set(sbr_odds["season"].to_list()) if s in OPEN_SEASONS)
    return held[1:]


def diagnostic(sbr_odds: pl.DataFrame, games: pl.DataFrame, draws: int = DRAWS) -> dict[str, Any]:
    """The walk-forward on every open season sbr_odds holds after its first, compared across the
    change of closing book: B0's E2 minus E1, B0 minus B1 per experiment, and each season's B1
    fits with their train_cutoff."""
    seasons = seasons_of(sbr_odds)
    predictions, _, fits = run(sbr_odds, games, seasons, [B1_METHOD])
    later = reports.e2_against_e1(predictions).filter(pl.col("model") == "B0")
    against_b1 = reports.against_baseline(predictions)
    return {
        "description": DESCRIPTION,
        "switch_season": SWITCH_SEASON,
        "method": B1_METHOD.value,
        "seasons": seasons,
        "b0_e2_minus_e1": _by_era(later, draws),
        "b0_minus_b1": {
            experiment: _by_era(against_b1.filter(pl.col("experiment") == experiment), draws)
            for experiment in sorted(fits)
        },
        "b1_fits": {
            experiment: {
                str(season): {
                    "intercept": fit.intercept,
                    "slope": fit.slope,
                    "games": fit.games,
                    "train_cutoff": fit.train_cutoff.isoformat(),
                }
                for season, fit in by_season.items()
            }
            for experiment, by_season in sorted(fits.items())
        },
    }
