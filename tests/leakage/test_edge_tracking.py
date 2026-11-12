"""Point-in-time rules for the NHL EDGE snapshots (#424): a season total counts from its fetch,
so a prediction sees only the copies fetched before it, a copy reused later the same day keeps
its first fetch time, and a later day's total, which takes in later games, is never stamped
earlier than its fetch."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from nhl_edge.ingest import nhl_api
from nhl_edge.ingest.edge_tracking import parse_edge_team, snapshot
from nhl_edge.ingest.nhl_api import NhlApi
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import EDGE_ENDPOINTS
from nhl_edge.lake.tables import known_at

FIXTURES = Path(__file__).parents[1] / "unit" / "fixtures" / "nhl_api" / "edge"
SEASON = 20262027
ZONE = "team-zone-time-details"
ZONE_TIME = (FIXTURES / f"{ZONE}_8_{SEASON}_2.json").read_bytes()
OFFENSIVE = "zoneTimeDetails[strengthCode=all].offensiveZonePctg"
# The nightly snapshot of two mornings, and a game between them.
FIRST = datetime(2026, 11, 12, 9, 5, tzinfo=UTC)
GAME_START = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
SECOND = datetime(2026, 11, 13, 9, 5, tzinfo=UTC)


def after_a_game() -> bytes:
    """The next morning's copy: the night's game moved the season total."""
    data = json.loads(ZONE_TIME)
    data["zoneTimeDetails"][0]["offensiveZonePctg"] = 0.43
    return json.dumps(data).encode()


def rows(body: bytes, observed_utc: datetime) -> pl.DataFrame:
    return parse_edge_team(
        ZONE,
        body,
        team="MTL",
        team_id=8,
        season=SEASON,
        game_type=2,
        observed_utc=observed_utc,
        raw_key=f"nhl/edge-{ZONE}/{SEASON}/8/2/{observed_utc:%Y%m%dT%H%M%SZ}",
    )


def snapshots() -> pl.DataFrame:
    return pl.concat([rows(ZONE_TIME, FIRST), rows(after_a_game(), SECOND)])


def offensive(frame: pl.DataFrame) -> list[float]:
    return frame.filter(pl.col("metric") == OFFENSIVE).sort("observed_utc")["value"].to_list()


def test_a_snapshot_at_or_after_the_prediction_time_is_excluded() -> None:
    assert known_at(snapshots(), FIRST).is_empty()
    assert offensive(known_at(snapshots(), SECOND)) == [0.4154762]
    assert offensive(known_at(snapshots(), SECOND + timedelta(seconds=1))) == [0.4154762, 0.43]
    usable = known_at(snapshots(), GAME_START)
    assert (usable["observed_utc"] < GAME_START).all()


def test_the_total_that_takes_in_a_game_is_not_seen_before_its_fetch() -> None:
    # The game of the 12th is in the total of the 13th's copy only: a prediction of that game,
    # or of any game before the copy's fetch, never sees it.
    before = known_at(snapshots(), GAME_START)
    assert offensive(before) == [0.4154762]
    assert set(before["observed_utc"].to_list()) == {FIRST}


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def serve(request: httpx.Request) -> httpx.Response:
    endpoint = request.url.path.split("/")[3]
    return httpx.Response(200, content=(FIXTURES / f"{endpoint}_8_{SEASON}_2.json").read_bytes())


def api(store: RawStore, clock: Clock, handler: Any = serve) -> NhlApi:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=nhl_api.BASE_URL)
    return NhlApi(store, client, min_interval_s=0.0, now=clock, sleep=lambda _: None)


def test_rows_are_observed_at_the_fetch_and_a_same_day_rerun_keeps_it(tmp_path: Path) -> None:
    store, clock = RawStore(tmp_path), Clock(FIRST)
    nhl = api(store, clock)
    first = nhl.edge_team(ZONE, 8, SEASON, 2)
    assert first.fetched_utc == FIRST
    assert (rows(first.body, first.fetched_utc)["observed_utc"] == FIRST).all()
    # A rerun at noon reuses the morning's copy, stamped with the morning's fetch, not noon's.
    clock.at = FIRST + timedelta(hours=3)
    rerun = nhl.edge_team(ZONE, 8, SEASON, 2)
    assert rerun.cached and rerun.fetched_utc == FIRST


def test_a_new_day_never_reuses_an_earlier_day_s_total(tmp_path: Path) -> None:
    store, clock = RawStore(tmp_path), Clock(FIRST)
    nhl = api(store, clock)
    nhl.edge_team(ZONE, 8, SEASON, 2)
    clock.at = SECOND
    later = nhl.edge_team(ZONE, 8, SEASON, 2)
    assert not later.cached and later.fetched_utc == SECOND


def test_a_snapshot_stamps_every_copy_with_its_own_fetch(tmp_path: Path) -> None:
    store, clock = RawStore(tmp_path), Clock(FIRST)
    report = snapshot(api(store, clock), {"MTL": 8}, SEASON)
    assert report.failed == [] and report.fetched == 6
    for endpoint in EDGE_ENDPOINTS:
        key = store.latest(f"nhl/edge-{endpoint}/{SEASON}/8/2")
        assert key is not None
        assert store.meta(key)["fetched_utc"] == FIRST.isoformat()
