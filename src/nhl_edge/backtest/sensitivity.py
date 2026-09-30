"""E2 on every opener, reported beside the E2 that refuses implausible openers (ADR 0007).

E2 refuses an opener whose de-vigged home probability is outside EXTREME_OPEN (#56), a rule that
reads the opener alone. This module reruns the backtest with the rule off, so the report shows
what the rule changes: the refused openers return to E2's scoring and to B1's E2 fits. E1 reads
the close and is the same in both runs.
"""

from typing import Any

import polars as pl

from nhl_edge.backtest import reports
from nhl_edge.backtest.market import Experiment
from nhl_edge.backtest.walk_forward import B1_METHOD, run

VARIANT = "with_every_opener"
DESCRIPTION = "E2 on every opener, including the implausible ones the main E2 refuses (ADR 0007)"


def every_opener(sbr_odds: pl.DataFrame, games: pl.DataFrame, seasons: list[int]) -> dict[str, Any]:
    """E2 rerun without the implausible-opener rule: its coverage, log losses and B1 fits, and E2
    against E1. B0 is scored under B1_METHOD only, since the de-vig methods are compared in the
    main report."""
    predictions, coverage, fits = run(
        sbr_odds, games, seasons, [B1_METHOD], refuse_implausible=False
    )
    return {
        VARIANT: {
            "description": DESCRIPTION,
            "E2": reports.experiment(Experiment.E2, predictions, coverage, fits),
            "e2_against_e1": reports.against_e1(predictions),
        }
    }
