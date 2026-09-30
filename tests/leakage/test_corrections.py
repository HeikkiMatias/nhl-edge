"""Point-in-time rule for the feed rechecks (#30, ADR 0004). A recheck is a copy fetched a week
after the one the per-game tables read, with any post-game corrections in it. It is stored under
<kind>-recheck so that no table ever parses it: an ingest or replay after the recheck builds the
same tables, row for row, as before it."""

import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
from feed_fixtures import OPENING_WEEK_GAMES
from test_nhl_ingest import OPENING, FakeNhl, fail, make_api

from nhl_edge.ingest.nhl_api import FEED_KINDS
from nhl_edge.ingest.nhl_ingest import Ingest
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.tables import FEED_TABLES, Lake

LATER = datetime(2030, 1, 1, tzinfo=UTC)


def corrected(kind: str) -> bytes:
    """A recheck unlike the first copy in every table: every goal to another scorer, every time
    on ice changed, and no shifts."""
    if kind == "shiftcharts":
        return json.dumps({"data": [], "total": 0}).encode()
    return json.dumps({"id": 0, "plays": [], "playerByGameStats": {}}).encode()


def build(store: RawStore, lake: Lake, *, offline: bool) -> dict[str, pl.DataFrame]:
    handler = fail if offline else FakeNhl()
    api = make_api(store, handler, offline=offline)
    Ingest(api=api, lake=lake, supabase=None, feeds=True, players=False, echo=lambda _: None).run(
        [OPENING]
    )
    return {table: lake.read(table) for table in FEED_TABLES}


def test_a_recheck_never_reaches_the_per_game_tables(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    lake = Lake(tmp_path / "lake")
    before = build(store, lake, offline=False)
    for game_id in OPENING_WEEK_GAMES:
        for kind in FEED_KINDS:
            key = f"{kind}-recheck/20102011/{game_id}/{LATER:%Y%m%dT%H%M%SZ}"
            store.put("nhl", key, corrected(kind), {"fetched_utc": LATER.isoformat()})
    # A replay, and an ingest that may reuse the cache, both read the first copies.
    replayed = build(store, Lake(tmp_path / "replay"), offline=True)
    again = build(store, Lake(tmp_path / "again"), offline=False)
    for table in FEED_TABLES:
        assert replayed[table].equals(before[table]), table
        assert again[table].equals(before[table]), table
