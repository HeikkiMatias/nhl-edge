"""Leakage guard for the challengers (#255, ADR 0038): a challenger reads the lake only point in
time, through a reviewed reader for each table it registered, at its bundle's input cutoff; and
no decision day on or before its registration is ever computed."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl
import pytest

from nhl_edge.ingest.games import result_public_utc
from nhl_edge.live import bundle as lb
from nhl_edge.live import challengers as lc

CUTOFF = datetime(2026, 10, 16, 16, 45, 30, tzinfo=UTC)
SECOND = timedelta(seconds=1)
UTC_US = pl.Datetime("us", "UTC")


class Lake:
    """A lake of one table per name."""

    def __init__(self, **tables: pl.DataFrame) -> None:
        self.tables = tables

    def read(self, table: str, seasons: Any = None) -> pl.DataFrame:
        return self.tables[table]


def test_the_odds_are_read_as_they_were_before_the_cutoff() -> None:
    # Quotes observed just before, at and just after the cutoff, with the columns set
    # afterwards: the closing proxy flag and the game a quote was matched to (Codex on #265).
    odds = pl.DataFrame(
        {
            "event_id": ["e1", "e1", "e1"],
            "snapshot_utc": [CUTOFF - SECOND, CUTOFF, CUTOFF + SECOND],
            "price_decimal": [1.9, 1.95, 2.0],
            "is_closing_proxy": [True, False, False],
            "game_id": [2026020101] * 3,
            "game_type": [2] * 3,
        },
        schema_overrides={"snapshot_utc": UTC_US, "game_type": pl.Int8},
    )
    reader = lc.PointInTime(Lake(odds_snapshots=odds), CUTOFF, ("odds_snapshots",))
    read = reader.read("odds_snapshots")
    assert read["price_decimal"].to_list() == [1.9]
    assert not set(lc.ODDS_AFTERWARDS) & set(read.columns)
    assert list(reader.reads) == ["odds_snapshots"]


def test_the_schedule_holds_only_played_games_and_the_slates_own() -> None:
    # The schedule holds only games that went on to be played (Codex on #265): an upcoming game
    # outside the slate, public or not, would reveal whether it was postponed.
    day = CUTOFF.date()
    played, slate, upcoming = 2026020090, 2026020101, 2026020120
    schedule = pl.DataFrame(
        {
            "game_id": [played, slate, upcoming],
            "game_date": [day - timedelta(days=1), day, day + timedelta(days=1)],
            "observed_utc": [CUTOFF - timedelta(days=3)] * 3,
        },
        schema_overrides={"observed_utc": UTC_US},
    )
    assert result_public_utc(day - timedelta(days=1)).replace(tzinfo=UTC) < CUTOFF
    reader = lc.PointInTime(Lake(schedule=schedule), CUTOFF, ("schedule",), (slate,))
    assert sorted(reader.read("schedule")["game_id"].to_list()) == [played, slate]


def test_results_are_read_only_once_public() -> None:
    games = pl.DataFrame(
        {"game_id": [1, 2, 3], "observed_utc": [CUTOFF - SECOND, CUTOFF, CUTOFF + SECOND]},
        schema_overrides={"observed_utc": UTC_US},
    )
    reader = lc.PointInTime(Lake(games=games), CUTOFF, ("games",))
    assert reader.read("games")["game_id"].to_list() == [1]


def test_a_table_is_read_only_when_registered_and_reviewed() -> None:
    lake = Lake(games=pl.DataFrame(), goalie_starts=pl.DataFrame())
    with pytest.raises(ValueError, match="not among the tables"):
        lc.PointInTime(lake, CUTOFF, ()).read("games")
    # A table without a reviewed reader can't be read, nor registered.
    with pytest.raises(ValueError, match="no point-in-time reader"):
        lc.PointInTime(lake, CUTOFF, ("goalie_starts",)).read("goalie_starts")
    with pytest.raises(ValueError, match="no point-in-time reader for goalie_starts"):
        replace(lc.REGISTRY[0], reads=("goalie_starts",))
    for challenger in lc.REGISTRY:
        assert set(challenger.reads) <= set(lc.READERS)


def test_no_day_on_or_before_the_registration_is_ever_computed() -> None:
    challenger = replace(lc.REGISTRY[0], registered_on=date(2026, 10, 15))
    assert not challenger.counts(date(2026, 10, 14))
    assert not challenger.counts(date(2026, 10, 15))
    assert challenger.counts(date(2026, 10, 16))

    class Store:
        def keys(self, prefix: str) -> list[str]:
            if prefix != "bundles/live":
                return []
            days = ("2026-10-14", "2026-10-15", "2026-10-16")
            return [f"bundles/live/{day}/manifest.json" for day in days]

    assert lc.due(Store(), (challenger,)) == [(challenger, date(2026, 10, 16))]  # type: ignore[arg-type]

    on_the_day = lb.Bundle({"day": "2026-10-15"}, {}, None, None)
    with pytest.raises(ValueError, match="never its evidence"):
        lc.run(challenger, on_the_day, "x", None, Lake(), "x", CUTOFF + SECOND)  # type: ignore[arg-type]
