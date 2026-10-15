"""Leakage guard for the challengers (#255, ADR 0038): a challenger reads the lake only point in
time, rows known before its bundle's input cutoff, from the tables it registered; and no decision
day on or before its registration is ever computed."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl
import pytest

from nhl_edge.live import bundle as lb
from nhl_edge.live import challengers as lc

CUTOFF = datetime(2026, 10, 16, 16, 45, 30, tzinfo=UTC)
SECOND = timedelta(seconds=1)


class Lake:
    """A lake of one table per name, each row known at its own instant."""

    def __init__(self, **tables: pl.DataFrame) -> None:
        self.tables = tables

    def read(self, table: str, seasons: Any = None) -> pl.DataFrame:
        return self.tables[table]


def known(column: str) -> pl.DataFrame:
    """Three rows known just before, at and just after the cutoff."""
    return pl.DataFrame(
        {"row": [1, 2, 3], column: [CUTOFF - SECOND, CUTOFF, CUTOFF + SECOND]},
        schema={"row": pl.Int64, column: pl.Datetime("us", "UTC")},
    )


def test_a_point_in_time_read_returns_only_rows_known_before_the_cutoff() -> None:
    lake = Lake(goalie_starts=known("observed_utc"), odds_snapshots=known("snapshot_utc"))
    reader = lc.PointInTime(lake, CUTOFF, ("goalie_starts", "odds_snapshots"))
    assert reader.read("goalie_starts")["row"].to_list() == [1]
    # The odds are known at their snapshot.
    assert reader.read("odds_snapshots")["row"].to_list() == [1]
    # Each read is recorded by the sha256 of its rows.
    assert sorted(reader.reads) == ["goalie_starts", "odds_snapshots"]


def test_a_challenger_reads_only_the_tables_it_registered() -> None:
    lake = Lake(goalie_starts=known("observed_utc"), players=known("fetched_utc"))
    with pytest.raises(ValueError, match="not among the tables"):
        lc.PointInTime(lake, CUTOFF, ()).read("goalie_starts")
    # A table with no time it became known at can't be read point in time.
    with pytest.raises(ValueError, match="can't be read point in time"):
        lc.PointInTime(lake, CUTOFF, ("players",)).read("players")


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
