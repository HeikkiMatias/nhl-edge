import json
import re
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl
import pytest
from fakes import MemoryBucket
from feed_fixtures import OPENING_WEEK_GAMES, feed

from nhl_edge.ingest import nhl_api
from nhl_edge.ingest.nhl_api import NhlApi, NotCachedError
from nhl_edge.ingest.nhl_ingest import (
    DateRange,
    Ingest,
    Season,
    parse_seasons,
    recent_days,
    yesterday_et,
)
from nhl_edge.ingest.players import boxscore_player_ids, roster_player_ids
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.supabase import Supabase
from nhl_edge.lake.tables import FEED_TABLES, Lake

FIXTURES = Path(__file__).parent / "fixtures" / "nhl_api"
OPENING_WEEK = (FIXTURES / "schedule_2010-10-07.json").read_bytes()
UPCOMING_WEEK = (FIXTURES / "schedule_2026-09-28.json").read_bytes()
ROSTER = (FIXTURES / "roster_PHX_20102011.json").read_bytes()
LANDING = (FIXTURES / "landing_8478402.json").read_bytes()
OPENING = DateRange(date(2010, 10, 7), date(2010, 10, 8))
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
# The fixture roster (five players) is served for every team, and the opening week's boxscores
# add everyone dressed.
PLAYERS = len(
    roster_player_ids(ROSTER).union(
        *(boxscore_player_ids(feed("boxscore", game_id)) for game_id in OPENING_WEEK_GAMES)
    )
)


@pytest.fixture(autouse=True)
def plain_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warnings read "warning: ..." outside GitHub Actions, which CI itself runs in."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


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
        # A shift chart is asked for by query string: cayenneExp=gameId=2010020003
        chart_id = int(request.url.params.get("cayenneExp", "gameId=0").removeprefix("gameId="))
        routes: list[tuple[str, str, Callable[[re.Match[str]], bytes]]] = [
            ("schedule", r"/v1/schedule/2011-02-15", lambda m: probe_body()),
            ("schedule", r"/v1/schedule/2010-10-07", lambda m: OPENING_WEEK),
            ("schedule", r"/v1/schedule/2026-09-29", lambda m: UPCOMING_WEEK),
            ("pbp", r"/v1/gamecenter/(\d+)/play-by-play", lambda m: game_feed("play-by-play", m)),
            ("boxscore", r"/v1/gamecenter/(\d+)/boxscore", lambda m: game_feed("boxscore", m)),
            ("shifts", r"/stats/rest/en/shiftcharts", lambda m: feed("shiftcharts", chart_id)),
            ("roster", r"/v1/roster/[A-Z]{3}/\d{8}", lambda m: ROSTER),
            ("landing", r"/v1/player/(\d+)/landing", lambda m: landing(int(m[1]))),
        ]
        for name, pattern, body in routes:
            match = re.fullmatch(pattern, request.url.path)
            if match:
                self.calls[name] += 1
                return httpx.Response(200, content=body(match))
        return httpx.Response(404)


def game_feed(kind: str, match: re.Match[str]) -> bytes:
    return feed(kind, int(match[1]))


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


def ingest(
    api: NhlApi,
    lake: Lake,
    lines: list[str],
    supabase: Supabase | None = None,
    *,
    feeds: bool = True,
) -> Ingest:
    return Ingest(
        api=api, lake=lake, supabase=supabase, feeds=feeds, players=True, echo=lines.append
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


def test_feeds_are_parsed_into_the_per_game_tables(tmp_path: Path) -> None:
    lines: list[str] = []
    lake = Lake(tmp_path / "lake")
    [summary] = ingest(make_api(RawStore(tmp_path / "raw"), FakeNhl()), lake, lines).run([OPENING])

    games = set(OPENING_WEEK_GAMES)
    for table in FEED_TABLES:
        assert set(lake.read(table)["game_id"]) == games, table
    coverage = lake.read("shift_coverage")
    assert coverage["complete"].all()
    assert lake.read("actual_lineups").height == 3 * 40
    assert (summary.shots, summary.shifts) == (
        lake.read("shots").height,
        lake.read("shifts").height,
    )
    assert summary.shift_charts_complete == 3
    assert lines[-1].endswith(
        f"; {summary.shots} shots, {summary.shifts} shifts, shift charts complete in 3 of 3 games"
    )
    # Partitioned like games, by season and game date.
    dates = {p.parent.name for p in (tmp_path / "lake" / "shots").rglob("*.parquet")}
    assert dates == {"game_date=2010-10-07", "game_date=2010-10-08"}


def test_no_feeds_leaves_the_per_game_tables_alone(tmp_path: Path) -> None:
    store, lake = RawStore(tmp_path / "raw"), Lake(tmp_path / "lake")
    ingest(make_api(store, FakeNhl()), lake, []).run([OPENING])
    before = {table: lake.read(table) for table in FEED_TABLES}
    fake, lines = FakeNhl(), []
    ingest(make_api(store, fake), lake, lines, feeds=False).run([OPENING])
    assert fake.calls["pbp"] + fake.calls["boxscore"] + fake.calls["shifts"] == 0
    for table in FEED_TABLES:
        assert lake.read(table).equals(before[table]), table
    assert "shifts" not in lines[-1]


def test_the_schedule_is_written_with_games_even_without_feeds(tmp_path: Path) -> None:
    lake = Lake(tmp_path / "lake")
    ingest(make_api(RawStore(tmp_path / "raw"), FakeNhl()), lake, [], feeds=False).run([OPENING])
    schedule, games = lake.read("schedule"), lake.read("games")
    assert schedule["game_id"].to_list() == games["game_id"].to_list() == list(OPENING_WEEK_GAMES)
    assert (schedule["observed_utc"] < games["observed_utc"]).all()


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
    for table in ("games", "schedule", "players", *FEED_TABLES):
        assert replayed.read(table).height > 0
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


def test_a_replay_leaves_a_date_alone_when_its_schedule_copy_has_a_game_not_final(
    tmp_path: Path,
) -> None:
    store, lake = RawStore(tmp_path / "raw"), Lake(tmp_path / "lake")
    ingest(make_api(store, FakeNhl()), lake, []).run([OPENING])
    tables = ("games", "schedule", *FEED_TABLES)
    before = {table: lake.read(table) for table in tables}
    # A later copy of the week, fetched while 2010-10-08's game was still being played (#109).
    week = json.loads(OPENING_WEEK)
    for day in week["gameWeek"]:
        for game in day["games"]:
            if day["date"] == "2010-10-08":
                game["gameState"] = "LIVE"
    meta = {"fetched_utc": "2026-09-29T00:00:00+00:00", "status": 200}
    store.put("nhl", "schedule/2010-10-07/20260929T000000Z", json.dumps(week).encode(), meta)
    lines: list[str] = []
    [summary] = ingest(make_api(store, fail, offline=True), lake, lines).run([OPENING])
    # 2010-10-08 keeps exactly what the first run wrote; 2010-10-07 is rebuilt from the new copy.
    held = pl.col("game_date") == date(2010, 10, 8)
    for table in tables:
        after = lake.read(table)
        assert after.filter(held).height > 0, table
        assert after.filter(held).equals(before[table].filter(held)), table
        assert after.filter(~held).height == before[table].filter(~held).height, table
    assert summary.written == 2
    assert summary.held == [date(2010, 10, 8)]
    assert any("left 2010-10-08 as they were" in line for line in lines)


def test_a_live_run_still_writes_a_date_with_a_game_not_final(tmp_path: Path) -> None:
    lines: list[str] = []
    window = DateRange(date(2026, 9, 29), date(2026, 9, 29))
    [summary] = ingest(make_api(RawStore(tmp_path / "raw"), FakeNhl()), Lake(tmp_path), lines).run(
        [window]
    )
    assert summary.held == []
    assert not any("as they were" in line for line in lines)


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


def test_recent_days_end_yesterday() -> None:
    now = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
    assert recent_days(now, 1) == DateRange(date(2026, 10, 7), date(2026, 10, 7))
    assert recent_days(now, 3) == DateRange(date(2026, 10, 5), date(2026, 10, 7))
    with pytest.raises(ValueError):
        recent_days(now, 0)


def test_a_fresh_runner_reuses_r2_instead_of_the_nhl_api(tmp_path: Path) -> None:
    """The nightly lookback revisits dates an earlier run already stored: everything comes back
    from R2, so the NHL API is not called and no raw response is stored twice."""
    bucket = MemoryBucket()
    first = make_api(RawStore(tmp_path / "laptop" / "raw", "b", bucket), FakeNhl())
    ingest(first, Lake(tmp_path / "laptop" / "lake", "b", bucket), []).run([OPENING])
    stored = set(bucket.objects)

    fresh = FakeNhl()
    runner_lake = Lake(tmp_path / "runner" / "lake", "b", bucket)
    runner = make_api(RawStore(tmp_path / "runner" / "raw", "b", bucket), fresh)
    [summary] = ingest(runner, runner_lake, []).run([OPENING])
    assert sum(fresh.calls.values()) == 0
    assert summary.written == 3
    assert set(bucket.objects) == stored
    assert runner_lake.read("players").height == PLAYERS


def test_players_already_known_are_not_fetched_again(tmp_path: Path) -> None:
    store, lake = RawStore(tmp_path / "raw"), Lake(tmp_path / "lake")
    ingest(make_api(store, FakeNhl()), lake, []).run([OPENING])
    # A fresh raw cache (the nightly runner) with the players table already in the lake.
    fresh = FakeNhl()
    ingest(make_api(RawStore(tmp_path / "raw2"), fresh), lake, []).run([OPENING])
    assert fresh.calls["landing"] == 0
    assert lake.read("players").height == PLAYERS
    assert lake.read("games").select(pl.len()).item() == 3
