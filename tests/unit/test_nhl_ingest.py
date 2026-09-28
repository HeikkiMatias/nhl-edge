import json
import re
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl
import pytest

from nhl_edge.ingest import nhl_api
from nhl_edge.ingest.nhl_api import NhlApi, NotCachedError
from nhl_edge.ingest.nhl_ingest import (
    DateRange,
    Ingest,
    Season,
    parse_seasons,
    yesterday_et,
)
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.supabase import Supabase
from nhl_edge.lake.tables import Lake

FIXTURES = Path(__file__).parent / "fixtures" / "nhl_api"
OPENING_WEEK = (FIXTURES / "schedule_2010-10-07.json").read_bytes()
UPCOMING_WEEK = (FIXTURES / "schedule_2026-09-28.json").read_bytes()
BOXSCORE = (FIXTURES / "boxscore_2010020003.json").read_bytes()
ROSTER = (FIXTURES / "roster_PHX_20102011.json").read_bytes()
LANDING = (FIXTURES / "landing_8478402.json").read_bytes()
OPENING = DateRange(date(2010, 10, 7), date(2010, 10, 8))
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
# Two forwards, two defensemen and a goalie per roster; 10 players dressed per boxscore.
PLAYERS = 15


def probe_body() -> bytes:
    """The 2010-11 probe, with the season cut to the two fixture days."""
    data = json.loads(OPENING_WEEK)
    data["regularSeasonEndDate"] = "2010-10-08"
    return json.dumps(data).encode()


def landing(player_id: int) -> bytes:
    return LANDING.replace(b"8478402", str(player_id).encode())


class FakeNhl:
    """Serves fixtures by path and counts requests by endpoint."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        routes: list[tuple[str, str, Callable[[re.Match[str]], bytes]]] = [
            ("schedule", r"/v1/schedule/2011-02-15", lambda m: probe_body()),
            ("schedule", r"/v1/schedule/2010-10-07", lambda m: OPENING_WEEK),
            ("schedule", r"/v1/schedule/2026-09-29", lambda m: UPCOMING_WEEK),
            ("pbp", r"/v1/gamecenter/(\d+)/play-by-play", lambda m: f'{{"id": {m[1]}}}'.encode()),
            ("boxscore", r"/v1/gamecenter/\d+/boxscore", lambda m: BOXSCORE),
            ("shifts", r"/stats/rest/en/shiftcharts", lambda m: b'{"data": [], "total": 0}'),
            ("roster", r"/v1/roster/[A-Z]{3}/\d{8}", lambda m: ROSTER),
            ("landing", r"/v1/player/(\d+)/landing", lambda m: landing(int(m[1]))),
        ]
        for name, pattern, body in routes:
            match = re.fullmatch(pattern, request.url.path)
            if match:
                self.calls[name] += 1
                return httpx.Response(200, content=body(match))
        return httpx.Response(404)


def fail(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"replay must not call {request.url}")


def make_api(
    store: RawStore,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    offline: bool = False,
) -> NhlApi:
    ticks = iter(range(100_000))
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=nhl_api.BASE_URL)
    return NhlApi(
        store,
        client,
        min_interval_s=0,
        now=lambda: NOW + timedelta(seconds=next(ticks)),
        offline=offline,
    )


def ingest(api: NhlApi, lake: Lake, lines: list[str], supabase: Supabase | None = None) -> Ingest:
    return Ingest(
        api=api, lake=lake, supabase=supabase, feeds=True, players=True, echo=lines.append
    )


def test_first_run_writes_games_players_and_caches_every_feed(tmp_path: Path) -> None:
    fake, lines = FakeNhl(), []
    lake = Lake(tmp_path / "lake")
    [summary] = ingest(make_api(RawStore(tmp_path / "raw"), fake), lake, lines).run([OPENING])

    games = lake.read("games")
    assert games["game_id"].to_list() == [2010020003, 2010020004, 2010020008]
    teams = set(games["home"]) | set(games["away"])
    assert lake.read("players").height == PLAYERS
    assert fake.calls == {
        "schedule": 1,
        "pbp": 3,
        "boxscore": 3,
        "shifts": 3,
        "roster": len(teams),
        "landing": PLAYERS,
    }
    assert (summary.listed, summary.written, summary.not_final) == (3, 3, [])
    assert summary.requests == sum(fake.calls.values())
    assert lines[-1].startswith("2010-10-07..2010-10-08: 3 regular-season games listed, 3 final")
    for kind in ("play-by-play", "boxscore", "shiftcharts"):
        assert len(list((tmp_path / "raw" / "nhl" / kind / "20102011").iterdir())) == 3


def test_rerun_is_served_from_the_cache(tmp_path: Path) -> None:
    store, lake = RawStore(tmp_path / "raw"), Lake(tmp_path / "lake")
    ingest(make_api(store, FakeNhl()), lake, []).run([OPENING])
    again = FakeNhl()
    [summary] = ingest(make_api(store, again), lake, []).run([OPENING])
    assert sum(again.calls.values()) == 0
    assert summary.written == 3


def test_replay_rebuilds_identical_tables_offline(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    online, replayed = Lake(tmp_path / "lake"), Lake(tmp_path / "rebuilt")
    ingest(make_api(store, FakeNhl()), online, []).run([OPENING])
    ingest(make_api(store, fail, offline=True), replayed, []).run([OPENING])
    for table in ("games", "players"):
        assert replayed.read(table).equals(online.read(table))


def test_replay_fails_on_a_cache_miss(tmp_path: Path) -> None:
    api = make_api(RawStore(tmp_path / "raw"), fail, offline=True)
    with pytest.raises(NotCachedError, match="nhl/schedule/2010-10-07"):
        ingest(api, Lake(tmp_path / "lake"), []).run([OPENING])


def test_games_not_final_are_skipped_with_a_warning(tmp_path: Path) -> None:
    lines: list[str] = []
    lake = Lake(tmp_path / "lake")
    window = DateRange(date(2026, 9, 29), date(2026, 9, 29))
    [summary] = ingest(make_api(RawStore(tmp_path / "raw"), FakeNhl()), lake, lines).run([window])
    assert (summary.listed, summary.written) == (5, 0)
    assert {state for _, state in summary.not_final} == {"FUT"}
    assert lake.read("games").is_empty()
    assert any(
        line.startswith("warning: 2026-09-29..2026-09-29: 5 games not final") for line in lines
    )


def test_season_window_uses_the_probe_bounds_and_checks_the_count(tmp_path: Path) -> None:
    fake, lines = FakeNhl(), []
    api = make_api(RawStore(tmp_path / "raw"), fake)
    lake = Lake(tmp_path / "lake")
    [summary] = ingest(api, lake, lines).run([Season(20102011)])
    assert summary.label == "20102011"
    assert summary.written == 3
    assert fake.calls["schedule"] == 2  # the probe, then the one week
    assert "warning: 20102011: 3 games written, expected 1230" in lines


def test_supabase_gets_the_games_and_a_ping(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200 if request.method == "GET" else 201, json=[])

    supabase = Supabase(
        "https://abc.supabase.co",
        "sb_secret_x",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    api = make_api(RawStore(tmp_path / "raw"), FakeNhl())
    ingest(api, Lake(tmp_path / "lake"), [], supabase).run([OPENING])
    upsert, ping = requests
    assert upsert.url.params["on_conflict"] == "game_id"
    assert [row["game_id"] for row in json.loads(upsert.content)] == [
        2010020003,
        2010020004,
        2010020008,
    ]
    assert json.loads(upsert.content)[0]["game_date"] == "2010-10-07"
    assert ping.method == "GET"


def test_empty_window_still_pings_supabase(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    supabase = Supabase(
        "https://abc.supabase.co",
        "sb_secret_x",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    api = make_api(RawStore(tmp_path / "raw"), FakeNhl())
    window = DateRange(date(2026, 9, 29), date(2026, 9, 29))
    ingest(api, Lake(tmp_path / "lake"), [], supabase).run([window])
    assert [r.method for r in requests] == ["GET"]


def test_parse_seasons() -> None:
    assert parse_seasons("20232024") == [20232024]
    assert parse_seasons("20102011-20122013, 20202021") == [20102011, 20112012, 20122013, 20202021]
    assert len(parse_seasons("20102011-20252026")) == 16
    for bad in ("2023", "20232025", "20252026-20102011"):
        with pytest.raises(ValueError):
            parse_seasons(bad)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 8, 9, 0, tzinfo=UTC), date(2026, 10, 7)),  # 05:00 EDT
        (datetime(2026, 12, 8, 9, 0, tzinfo=UTC), date(2026, 12, 7)),  # 04:00 EST
        (datetime(2026, 10, 8, 3, 30, tzinfo=UTC), date(2026, 10, 6)),  # still Oct 7 in ET
    ],
)
def test_yesterday_is_the_eastern_date(now: datetime, expected: date) -> None:
    assert yesterday_et(now) == expected


def test_players_already_known_are_not_fetched_again(tmp_path: Path) -> None:
    store, lake = RawStore(tmp_path / "raw"), Lake(tmp_path / "lake")
    ingest(make_api(store, FakeNhl()), lake, []).run([OPENING])
    # A fresh raw cache (the nightly runner) with the players table already in the lake.
    fresh = FakeNhl()
    ingest(make_api(RawStore(tmp_path / "raw2"), fresh), lake, []).run([OPENING])
    assert fresh.calls["landing"] == 0
    assert lake.read("players").height == PLAYERS
    assert lake.read("games").select(pl.len()).item() == 3
