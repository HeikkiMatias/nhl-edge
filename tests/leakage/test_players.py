"""players has no observed_utc because it may only hold facts fixed before a player's NHL debut.
A time-varying column (team, stats, status) must go to a table with observed_utc instead, so the
column set is locked here and the strict schema rejects anything extra."""

from datetime import UTC, date, datetime

import pandera.errors
import polars as pl
import pytest

from nhl_edge.lake.schemas import Players, dtypes

STATIC_BIO = [
    "player_id",
    "name",
    "birth_date",
    "position",
    "shoots",
    "draft_year",
    "draft_overall",
    "fetched_utc",
    "raw_key",
]


def test_players_holds_only_static_bio_columns() -> None:
    assert list(dtypes(Players)) == STATIC_BIO


def test_a_time_varying_column_is_rejected() -> None:
    row = {
        "player_id": 8478402,
        "name": "Connor McDavid",
        "birth_date": date(1997, 1, 13),
        "position": "C",
        "shoots": "L",
        "draft_year": 2015,
        "draft_overall": 1,
        "fetched_utc": datetime(2026, 9, 28, tzinfo=UTC),
        "raw_key": "k",
    }
    frame = pl.DataFrame([row], schema=dtypes(Players))
    Players.validate(frame)
    with pytest.raises(pandera.errors.SchemaError):
        Players.validate(frame.with_columns(pl.lit("EDM").alias("current_team")))
