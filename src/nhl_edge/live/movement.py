"""Entry to close (#251): does a model's disagreement with the market at the decision predict the
market's move to the close?

On history the bets anticipated the market: its fair price moved toward them by +2.83%
[+2.04%, +3.62%], yet their CLV was negative (`reports/backtest/accepted.json`). That was SBR's
opener to its close. Live bets are made at Pinnacle's 12:45 ET price, and the bets are a small,
selected sample, so this asks the question of every predicted slate game with a close.

For each such game, a row with:
- **p_decision:** Pinnacle's decision pair (the ledger's home_price and away_price), de-vigged
  multiplicatively (market/devig.py, hard rule 3);
- **p_close:** its closing proxy's pair (live/settle.closes, ADR 0033), de-vigged the same way, and
  only from a proxy taken after the decision's own snapshot, as settlement takes it;
- **move:** the close less the decision, in the home side's log-odds;
- **each model's disagreement** at the decision (d_B3, d_BLEND): its log-odds less the decision's.

Every input comes from the ledger as written. The close is an outcome here, never an input, and
nothing here feeds a decision.
"""

import numpy as np
import polars as pl

from nhl_edge.market import closing
from nhl_edge.market.devig import fair_probabilities

# Each model's disagreement column and its probability in the ledger.
MODELS = {"B3": "p_b3", "BLEND": "p_blend"}


def _fair_home(frame: pl.DataFrame, home: str, away: str) -> pl.Series:
    """The home side's fair probability of each row's pair (home, away), null where either is."""
    known = (frame[home].is_not_null() & frame[away].is_not_null()).to_numpy()
    fair = np.full(frame.height, np.nan)
    if known.any():
        pairs = frame.select(home, away).to_numpy()[known]
        fair[known] = fair_probabilities(pairs)[:, 0]
    return pl.Series(fair, dtype=pl.Float64).fill_nan(None)


def _logit(p: pl.Expr) -> pl.Expr:
    return (p / (1 - p)).log()


def table(rows: pl.DataFrame, closes: pl.DataFrame) -> pl.DataFrame:
    """One row per predicted ledger row (rows: event_id, decision_snapshot_utc, the decision
    pair, B3, the blend and u_sd), with its close from closes (live/settle.closes): its status,
    and for a valid proxy the fair probabilities, the move and each model's disagreement. A
    proxy taken at or before the decision's snapshot is no close (MISSING), as in settlement."""
    joined = rows.join(closes, on="event_id", how="left").with_columns(
        close_status=pl.col("close_status").fill_null(closing.MISSING)
    )
    after = pl.col("close_snapshot_utc") > pl.col("decision_snapshot_utc")
    usable = (pl.col("close_status") == closing.PROXY) & after
    joined = joined.with_columns(
        close_status=pl.when((pl.col("close_status") == closing.PROXY) & ~after)
        .then(pl.lit(closing.MISSING))
        .otherwise(pl.col("close_status")),
        **{
            name: pl.when(usable).then(pl.col(name))
            for name in ("close_snapshot_utc", "close_home", "close_away")
        },
    )
    decided = joined.with_columns(
        p_decision=_fair_home(joined, "home_price", "away_price"),
        p_close=_fair_home(joined, "close_home", "close_away"),
    )
    return decided.select(
        "season",
        "game_date",
        "game_id",
        "start_utc",
        "decision_snapshot_utc",
        "close_status",
        "close_snapshot_utc",
        lead_min=(pl.col("start_utc") - pl.col("close_snapshot_utc")).dt.total_seconds() / 60,
        p_decision="p_decision",
        p_close="p_close",
        move=_logit(pl.col("p_close")) - _logit(pl.col("p_decision")),
        u_sd="u_sd",
        **{
            f"d_{name}": _logit(pl.col(column)) - _logit(pl.col("p_decision"))
            for name, column in MODELS.items()
        },
    ).sort("game_date", "game_id")
