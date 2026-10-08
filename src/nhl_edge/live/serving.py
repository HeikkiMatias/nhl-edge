"""The paper ledger in Supabase for the dashboard (#167, docs/plans/phase-5.md task 8).

The R2 ledgers (ledger/live/<date>.parquet) are the record; Supabase holds a copy the dashboard
reads (supabase/migrations/20261008120000_paper_ledger.sql):
- **predictions:** every slate game's decision, inserted once and never rewritten.
- **paper_bets:** every bet as decided, inserted once; its settlement columns (nhl live settle) are
  set afterwards, the only columns the service role may update.

`nhl live supabase --r2` copies whatever is new, so a rerun or a backfill of the whole season is
the same command. Nothing here feeds a decision.
"""

from typing import Any

import polars as pl

from nhl_edge.lake.supabase import Supabase

PREDICTIONS = "predictions"
PAPER_BETS = "paper_bets"
KEY = ("game_date", "game_id")
PREDICTION_COLUMNS = (
    "game_date",
    "season",
    "game_id",
    "event_id",
    "start_utc",
    "home",
    "away",
    "prediction_utc",
    "published_utc",
    "status",
    "decision_snapshot_utc",
    "home_price",
    "away_price",
    "last_update_utc",
    "best_home_price",
    "best_home_book",
    "best_away_price",
    "best_away_book",
    "p_b0",
    "p_b1",
    "p_b2",
    "p_b3",
    "u",
    "u_sd",
    "p_blend",
    "p_blend_b2",
    "p_blend_market",
    "bet",
    "policy_version",
    "blend_version",
    "feature_build",
    "code_version",
)
BET_COLUMNS = (
    "game_date",
    "game_id",
    "start_utc",
    "home",
    "away",
    "prediction_utc",
    "decision_snapshot_utc",
    "side",
    "price",
    "p_side",
    "ev",
    "hurdle",
    "fraction",
    "bankroll",
    "stake",
    "policy_version",
    "blend_version",
)
# paper_settlements' column for each of paper_bets' settlement columns.
SETTLEMENT_COLUMNS = {
    "settled_utc": "settled_utc",
    "settlement": "status",
    "won": "won",
    "profit": "profit",
    "close_status": "close_status",
    "close_snapshot_utc": "close_snapshot_utc",
    "p_close": "p_close",
    "clv": "clv",
    "fair_move": "fair_move",
    "settle_version": "code_version",
}


def prediction_rows(ledger: pl.DataFrame) -> pl.DataFrame:
    """Every ledger row as a predictions row."""
    return ledger.with_columns(pl.col("bet").fill_null(False)).select(PREDICTION_COLUMNS)


def bet_rows(ledger: pl.DataFrame) -> pl.DataFrame:
    """Every bet of the ledger as a paper_bets row, without its settlement."""
    return ledger.filter(pl.col("bet").fill_null(False)).select(BET_COLUMNS)


def settlement_rows(settlements: pl.DataFrame) -> pl.DataFrame:
    """Each settled or voided bet's key and settlement columns, as paper_bets names them."""
    return settlements.select(
        *KEY, **{column: source for column, source in SETTLEMENT_COLUMNS.items()}
    )


def sync(supabase: Supabase, ledger: pl.DataFrame, settlements: pl.DataFrame) -> dict[str, Any]:
    """Insert the ledger's new predictions and bets, then set every settlement on its bet. Returns
    the counts sent."""
    predictions = prediction_rows(ledger)
    bets = bet_rows(ledger)
    # Predictions first: each bet refers to its prediction.
    sent = {
        PREDICTIONS: supabase.insert_new(PREDICTIONS, predictions, KEY),
        PAPER_BETS: supabase.insert_new(PAPER_BETS, bets, KEY),
    }
    settled = settlement_rows(settlements).join(bets.select(KEY), on=list(KEY))
    for row in settled.iter_rows(named=True):
        supabase.update(
            PAPER_BETS,
            {key: row[key] for key in KEY},
            {column: row[column] for column in SETTLEMENT_COLUMNS},
        )
    sent["settlements"] = settled.height
    return sent
