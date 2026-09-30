"""E2's sensitivity to the suspect SBR openers (#56, reference/sbr_suspect_openers.csv).

Whether E2 drops, fixes or keeps these openers is the owner's decision. Until then E2 keeps every
game, and this module only reports what dropping them would change. Each variant reruns the
backtest with the listed games' opening moneylines removed, so they leave E2's scoring and B1's E2
fits. E1 reads the close and is unchanged.

The list reads each game's close, which is public only at its start, and its thresholds were set
on every training and development season (ingest/sbr_suspect.py). So a variant is hindsight
throughout. Removing a test-season game uses that game's own close, and removing an earlier
season's game from B1's fit uses criteria chosen with the test season in view. A variant says how
much of E2's result rests on those prices, and is never a filter a live prediction could apply or
a bar a later model must clear.
"""

from collections.abc import Iterable
from typing import Any, NamedTuple

import polars as pl

from nhl_edge.backtest import reports
from nhl_edge.backtest.market import Experiment
from nhl_edge.backtest.walk_forward import B1_METHOD, run

NOTE = (
    "Hindsight: the list reads each game's close and its thresholds were set on the training and "
    "development seasons. This sizes E2's data problem and is not a tradable E2 result."
)


class Variant(NamedTuple):
    description: str
    selects: pl.Expr


VARIANTS = {
    "without_suspect_openers": Variant(
        "E2 without every listed opener, the option the #56 thread recommends", pl.lit(True)
    ),
    "without_proven_errors": Variant(
        "E2 without the openers the source cannot have meant (extreme_open, swapped or "
        "below_100), keeping those flagged only for a big move",
        pl.col("extreme_open") | pl.col("swapped") | pl.col("below_100"),
    ),
}


def listed(suspects: pl.DataFrame, variant: str) -> pl.DataFrame:
    """The game_id and season of each opener the variant removes."""
    return suspects.filter(VARIANTS[variant].selects).select("game_id", "season")


def without_openers(sbr_odds: pl.DataFrame, game_ids: Iterable[int]) -> pl.DataFrame:
    """sbr_odds without the opening moneylines of these games. Their closes, puck lines and
    totals stay."""
    opener = (
        (pl.col("market") == "h2h")
        & (pl.col("quote") == "open")
        & pl.col("game_id").is_in(list(game_ids))
    )
    return sbr_odds.filter(~opener)


def sensitivities(
    sbr_odds: pl.DataFrame,
    games: pl.DataFrame,
    seasons: list[int],
    suspects: pl.DataFrame,
) -> dict[str, Any]:
    """Each variant's E2 report, rerun on sbr_odds without its openers: the openers removed per
    season read, E2's coverage, log losses and B1 fits, and E2 against E1. B0 is scored under
    B1_METHOD only, since the de-vig methods are compared in the main report."""
    read = sorted(sbr_odds["season"].unique().to_list())
    out: dict[str, Any] = {}
    for name, variant in VARIANTS.items():
        removed = listed(suspects, name).filter(pl.col("season").is_in(read))
        predictions, coverage, fits = run(
            without_openers(sbr_odds, removed["game_id"]), games, seasons, [B1_METHOD]
        )
        counts = removed.group_by("season").len().sort("season")
        out[name] = {
            "description": variant.description,
            "note": NOTE,
            "removed_openers": {str(s): n for s, n in counts.iter_rows()},
            "E2": reports.experiment(Experiment.E2, predictions, coverage, fits),
            "e2_against_e1": reports.against_e1(predictions),
        }
    return out
