"""The paper ledger copied to Supabase for the dashboard (#167): predictions and bets inserted
once, settlements set on their bets, and the columns sent are the migration's."""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import predict_fixtures as pf

from nhl_edge.live import predict as lp
from nhl_edge.live import serving

MIGRATION = Path("supabase/migrations/20261008120000_paper_ledger.sql").read_text()


def table_columns(table: str) -> set[str]:
    """The columns of a create table statement in the migration, but its id and created_at."""
    body = re.search(rf"create table public\.{table} \((.*?)\n\);", MIGRATION, re.S)
    assert body is not None
    types = "bigint|integer|text|date|timestamptz|numeric|double precision|boolean|uuid"
    column = re.compile(rf"^  ([a-z_0-9]+) (?:{types})\b")
    found = {m.group(1) for line in body.group(1).splitlines() if (m := column.match(line))}
    return found - {"id", "created_at"}


def ledger() -> pl.DataFrame:
    inputs = pf.day()
    return lp.ledger(lp.decide(inputs), inputs)


class FakeSupabase:
    def __init__(self) -> None:
        self.inserted: dict[str, pl.DataFrame] = {}
        self.updates: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def insert_new(self, table: str, frame: pl.DataFrame, key: Any) -> int:
        assert tuple(key) == serving.KEY
        self.inserted[table] = frame
        return frame.height

    def update(self, table: str, match: dict[str, Any], values: dict[str, Any]) -> None:
        self.updates.append((table, match, values))


def test_the_rows_sent_are_the_migrations_columns() -> None:
    assert set(serving.PREDICTION_COLUMNS) == table_columns("predictions")
    bets = table_columns("paper_bets")
    assert set(serving.BET_COLUMNS) | set(serving.SETTLEMENT_COLUMNS) == bets
    # The service role may update the settlement columns only.
    granted = re.search(r"grant update \((.*?)\) on table public\.paper_bets", MIGRATION, re.S)
    assert granted is not None
    assert {c.strip() for c in granted.group(1).split(",")} == set(serving.SETTLEMENT_COLUMNS)


def test_predictions_and_bets_are_inserted_then_settlements_set() -> None:
    decided = ledger()
    settled_utc = datetime(2026, 10, 8, 9, 7, tzinfo=UTC)
    first = decided.filter(pl.col("bet")).row(0, named=True)
    settlements = pl.DataFrame(
        {
            "game_date": [first["game_date"]],
            "game_id": [first["game_id"]],
            "settled_utc": [settled_utc],
            "status": ["settled"],
            "won": [True],
            "profit": [1.2],
            "close_status": ["proxy"],
            "close_snapshot_utc": [first["start_utc"]],
            "p_close": [0.52],
            "clv": [0.03],
            "fair_move": [0.01],
            "code_version": ["settle-20261009-abc1234"],
        }
    )
    fake = FakeSupabase()
    sent = serving.sync(fake, decided, settlements)  # type: ignore[arg-type]
    assert sent == {
        serving.PREDICTIONS: decided.height,
        serving.PAPER_BETS: decided.filter(pl.col("bet")).height,
        "settlements": 1,
    }
    assert list(fake.inserted) == [serving.PREDICTIONS, serving.PAPER_BETS]
    assert fake.inserted[serving.PREDICTIONS].columns == list(serving.PREDICTION_COLUMNS)
    assert fake.inserted[serving.PAPER_BETS].columns == list(serving.BET_COLUMNS)
    ((table, match, values),) = fake.updates
    assert table == serving.PAPER_BETS
    assert match == {"game_date": first["game_date"], "game_id": first["game_id"]}
    assert values["settlement"] == "settled" and values["settle_version"].startswith("settle-")
    assert set(values) == set(serving.SETTLEMENT_COLUMNS)


def test_a_settlement_without_its_bet_in_the_ledger_is_not_sent() -> None:
    decided = ledger()
    stray = pl.DataFrame(
        {"game_date": [decided["game_date"][0]], "game_id": [1]},
        schema={"game_date": pl.Date, "game_id": pl.Int64},
    ).with_columns(**{source: pl.lit(None) for source in serving.SETTLEMENT_COLUMNS.values()})
    fake = FakeSupabase()
    assert serving.sync(fake, decided, stray)["settlements"] == 0  # type: ignore[arg-type]
