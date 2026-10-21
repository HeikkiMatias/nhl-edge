"""Point-in-time rules for shifts built from the time-on-ice reports (#68). They are the facts a
shift chart gives, so they count as public with the game's other feeds, at 10:00 UTC the morning
after its game date (ADR 0003, ADR 0004), however long after the game the reports were fetched.
They fill the chart's tables with the same columns; only raw_key tells where they came from."""

import json
from datetime import timedelta
from pathlib import Path

import polars as pl
import pytest
from toi_fixtures import (
    STAMP,
    TOI_GAME,
    TOI_SEASON,
    toi_boxscore,
    toi_feeds,
    toi_game,
    toi_games_row,
    toi_key,
    toi_page,
)

from nhl_edge.ingest.games import result_public_utc
from nhl_edge.ingest.nhl_api import parse_utc
from nhl_edge.ingest.nhl_ingest import parse_feeds
from nhl_edge.ingest.toi_reports import CachedReports
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import MIN_RESULT_LAG
from nhl_edge.lake.tables import TABLES, known_at

# When the reports were fetched, a year and a half after the game: not when its shifts were public.
FETCHED = parse_utc("2026-10-02T12:00:00Z")


def report_tables(tmp_path: Path) -> dict[str, pl.DataFrame]:
    """The fixture game parsed as nhl ingest does, its empty chart replaced by its two reports."""
    store = RawStore(tmp_path)
    for side in ("home", "visitor"):
        meta = {"fetched_utc": FETCHED.isoformat()}
        store.put("nhl", f"toi-{side}/{TOI_SEASON}/{TOI_GAME}/{STAMP}", toi_page(side), meta)
    return parse_feeds(toi_games_row(), toi_feeds(), CachedReports(store, print))


@pytest.mark.parametrize("table", ["shifts", "shift_coverage", "strength_time"])
def test_a_game_never_sees_its_own_report_shifts(tmp_path: Path, table: str) -> None:
    """No row is known at the game's start, six hours later, or at 10:00 UTC the next morning
    itself; every row is known right after, and none waits for the fetch."""
    rows = report_tables(tmp_path)[table]
    assert rows.height > 0
    start = parse_utc(json.loads(toi_boxscore())["startTimeUTC"])
    public = result_public_utc(toi_game().game_date)
    for moment in (start, start + MIN_RESULT_LAG, public):
        assert known_at(rows, moment).is_empty(), moment
    assert known_at(rows, public + timedelta(seconds=1)).height == rows.height
    assert (rows["observed_utc"] == public).all()


def test_report_shifts_keep_the_charts_columns(tmp_path: Path) -> None:
    tables = report_tables(tmp_path)
    for table in ("shifts", "shift_coverage", "strength_time"):
        assert tables[table].schema == TABLES[table].empty().schema, table
    assert tables["shifts"].columns == [
        "game_id",
        "season",
        "game_date",
        "team",
        "player_id",
        "period",
        "shift_number",
        "start_s",
        "end_s",
        "observed_utc",
        "raw_key",
    ]
    assert set(tables["shifts"]["raw_key"]) == {toi_key("toi-home"), toi_key("toi-visitor")}
    assert tables["shift_coverage"]["raw_key"].to_list() == [toi_key("toi-home")]
