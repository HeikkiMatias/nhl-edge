"""E2's sensitivity to implausible SBR openers (#56).

#56 lists 40 SBR openers that are likely wrong. Most of its flags (big_move, swapped) read the
close, which is public only at the start, so dropping those games from E2 would choose E2's sample
with information its opener prediction does not have (hard rule 1). E2 keeps them.

One criterion reads the opener alone: a de-vigged home probability outside EXTREME_OPEN. A live
E2 could refuse such an opener when it reads it, so the rule is point-in-time. (Openers that sum
below 100% are already refused by de-vigging.) Whether E2 applies the rule is the owner's
decision. Until then E2 keeps every game, and this module reruns the backtest without the
implausible openers to report what the rule would change: they leave E2's scoring and B1's E2
fits, and E1, which reads the close, is unchanged.

EXTREME_OPEN's bounds were set with the training and development seasons' closes in view (none is
outside 0.21 to 0.84), so they are a development choice, to be fixed before a held-out season is
read.
"""

from collections.abc import Iterable
from typing import Any

import polars as pl

from nhl_edge.backtest import reports
from nhl_edge.backtest.market import Experiment, market_prices
from nhl_edge.backtest.walk_forward import B1_METHOD, b0, run
from nhl_edge.ingest.sbr_suspect import EXTREME_OPEN

VARIANT = "without_implausible_openers"
DESCRIPTION = (
    f"E2 without the openers whose de-vigged home probability is outside {EXTREME_OPEN[0]} to "
    f"{EXTREME_OPEN[1]}, a rule that reads the opener alone. Its bounds were set with the "
    "training and development seasons' closes in view"
)


def implausible(sbr_odds: pl.DataFrame) -> pl.DataFrame:
    """The game_id and season of each E2 opener whose de-vigged home probability under
    B1_METHOD is outside EXTREME_OPEN. It reads only the price E2 reads at its prediction time."""
    low, high = EXTREME_OPEN
    fair = b0(market_prices(sbr_odds, Experiment.E2), B1_METHOD)
    outside = (pl.col("p_home") < low) | (pl.col("p_home") > high)
    return fair.filter(outside).select("game_id", "season").sort("game_id")


def without_openers(sbr_odds: pl.DataFrame, game_ids: Iterable[int]) -> pl.DataFrame:
    """sbr_odds without the opening moneylines of these games. Their closes, puck lines and
    totals stay."""
    opener = (
        (pl.col("market") == "h2h")
        & (pl.col("quote") == "open")
        & pl.col("game_id").is_in(list(game_ids))
    )
    return sbr_odds.filter(~opener)


def sensitivity(sbr_odds: pl.DataFrame, games: pl.DataFrame, seasons: list[int]) -> dict[str, Any]:
    """E2 rerun on sbr_odds without the implausible openers: the openers removed per season read,
    E2's coverage, log losses and B1 fits, and E2 against E1. B0 is scored under B1_METHOD only,
    since the de-vig methods are compared in the main report."""
    removed = implausible(sbr_odds)
    predictions, coverage, fits = run(
        without_openers(sbr_odds, removed["game_id"]), games, seasons, [B1_METHOD]
    )
    counts = removed.group_by("season").len().sort("season")
    return {
        VARIANT: {
            "description": DESCRIPTION,
            "removed_openers": {str(s): n for s, n in counts.iter_rows()},
            "E2": reports.experiment(Experiment.E2, predictions, coverage, fits),
            "e2_against_e1": reports.against_e1(predictions),
        }
    }
