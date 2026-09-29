import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pandera.errors
import polars as pl
import pytest

from nhl_edge.ingest import nhl_api
from nhl_edge.ingest.nhl_api import NhlApi, NotCachedError, ScheduledGame, scheduled_games
from nhl_edge.ingest.pregame import (
    games_to_poll,
    parse_pregame_goalies,
    replay_pregame_goalies,
    run_poll,
)
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import PregameGoalies
from nhl_edge.lake.tables import Lake

FIXTURES = Path(__file__).parent / "fixtures" / "nhl_api"
# FLA at CAR, the 2026-27 opener, fetched about nine hours before its 21:00 UTC start.
BOXSCORE = (FIXTURES / "pregame-boxscore_2026020001.json").read_bytes()
WEEK = (FIXTURES / "schedule_2026-09-28.json").read_bytes()
START = datetime(2026, 9, 29, 21, 0, tzinfo=UTC)
MORNING = datetime(2026, 9, 29, 11, 5, tzinfo=UTC)
CAR_STARTER, FLA_STARTER, FLA_BACKUP = 8483548, 8474593, 8481033


def with_goalies(home: list[dict[str, object]], away: list[dict[str, object]]) -> bytes:
    """The real pre-game response with goalies added, as the boxscore lists them once the
    lineups are in (the post-game format: playerByGameStats.<side>.goalies[].starter)."""
    data = json.loads(BOXSCORE)
    data["playerByGameStats"] = {"homeTeam": {"goalies": home}, "awayTeam": {"goalies": away}}
    return json.dumps(data).encode()


def goalie(player_id: int, starter: bool) -> dict[str, object]:
    return {"playerId": player_id, "starter": starter}


def test_hours_before_the_game_no_goalie_is_listed() -> None:
    frame = parse_pregame_goalies(BOXSCORE, MORNING, "nhl/pregame-boxscore/x")
    assert frame.height == 2
    assert frame.select("team", "is_home").rows() == [("CAR", True), ("FLA", False)]
    assert frame["goalies_listed"].to_list() == [0, 0]
    assert frame["starter_id"].null_count() == 2
    assert (frame["observed_utc"] == MORNING).all()
    assert (frame["start_utc"] == START).all()
    assert frame["game_state"].to_list() == ["FUT", "FUT"]
    assert frame["season"].to_list() == [20262027, 20262027]


def test_the_flagged_starter_is_recorded() -> None:
    body = with_goalies(
        [goalie(CAR_STARTER, True), goalie(8481611, False)],
        [goalie(FLA_STARTER, True), goalie(FLA_BACKUP, False)],
    )
    frame = parse_pregame_goalies(body, START - timedelta(minutes=30), "k")
    assert frame["starter_id"].to_list() == [CAR_STARTER, FLA_STARTER]
    assert frame["goalies_listed"].to_list() == [2, 2]


def test_no_starter_when_none_or_both_are_flagged() -> None:
    body = with_goalies(
        [goalie(CAR_STARTER, False), goalie(8481611, False)],
        [goalie(FLA_STARTER, True), goalie(FLA_BACKUP, True)],
    )
    frame = parse_pregame_goalies(body, START - timedelta(minutes=30), "k")
    assert frame["starter_id"].to_list() == [None, None]
    assert frame["starters_flagged"].to_list() == [0, 2]


def test_a_response_fetched_at_or_after_the_start_gives_no_rows() -> None:
    assert parse_pregame_goalies(BOXSCORE, START, "k").is_empty()
    assert parse_pregame_goalies(BOXSCORE, START + timedelta(minutes=5), "k").is_empty()


def test_schema_rejects_rows_observed_at_the_start() -> None:
    frame = parse_pregame_goalies(BOXSCORE, MORNING, "k").with_columns(
        observed_utc=pl.col("start_utc")
    )
    with pytest.raises(pandera.errors.SchemaError):
        PregameGoalies.validate(frame)


def test_schema_rejects_a_starter_without_a_flag() -> None:
    frame = parse_pregame_goalies(BOXSCORE, MORNING, "k").with_columns(
        starter_id=pl.lit(CAR_STARTER, pl.Int64)
    )
    with pytest.raises(pandera.errors.SchemaError):
        PregameGoalies.validate(frame)


def test_poll_window_keeps_upcoming_regular_season_games_within_the_horizon() -> None:
    games = scheduled_games(WEEK)
    preseason = ScheduledGame(2026010099, 1, START + timedelta(hours=1), "BOS", "NYR")
    picked = games_to_poll([*games, preseason], MORNING)
    assert [game.game_id for game in picked] == [
        2026020001,
        2026020002,
        2026020003,
        2026020004,
        2026020005,
    ]
    # A pre-start poll 50 minutes before FLA at CAR reaches only that game.
    soon = games_to_poll(games, START - timedelta(minutes=50), timedelta(minutes=75))
    assert [game.game_id for game in soon] == [2026020001]
    # Once FLA at CAR is under way it is no longer polled.
    later = games_to_poll(games, START)
    assert 2026020001 not in {game.game_id for game in later}


def fake_nhl(store: RawStore, now: datetime) -> tuple[NhlApi, list[str]]:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.startswith("/v1/schedule/"):
            return httpx.Response(200, content=WEEK)
        if request.url.path.endswith("/boxscore"):
            return httpx.Response(200, content=BOXSCORE)
        return httpx.Response(200, content=b'{"matchup": {}}')

    calls = iter(range(10_000))
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=nhl_api.BASE_URL)
    api = NhlApi(
        store,
        client,
        min_interval_s=0.0,
        now=lambda: now + timedelta(seconds=next(calls)),
        sleep=lambda _: None,
    )
    return api, paths


def test_poll_stores_pregame_responses_apart_from_the_postgame_boxscores(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    api, paths = fake_nhl(store, MORNING)
    messages: list[str] = []
    report = run_poll(nhl=api, now=MORNING, echo=messages.append)
    assert report.games == 5 and report.teams == 10 and report.flagged == 0
    assert paths.count("/v1/gamecenter/2026020001/boxscore") == 1
    assert paths.count("/v1/gamecenter/2026020001/landing") == 1
    assert paths.count("/v1/gamecenter/2026020001/right-rail") == 1
    assert store.latest("nhl/pregame-boxscore/2026-09-29/2026020001") is not None
    assert store.latest("nhl/pregame-landing/2026-09-29/2026020001") is not None
    assert store.latest("nhl/pregame-right-rail/2026-09-29/2026020001") is not None
    # The ingest's boxscore cache stays empty, so a pre-game copy never stands in for the final.
    assert store.latest("nhl/boxscore/20262027/2026020001") is None
    offline = NhlApi(store, offline=True)
    with pytest.raises(NotCachedError):
        offline.boxscore(20262027, 2026020001)


def test_every_poll_fetches_afresh(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    api, paths = fake_nhl(store, MORNING)
    run_poll(nhl=api, now=MORNING, echo=lambda _: None)
    later = MORNING + timedelta(hours=6)
    api, paths = fake_nhl(store, later)
    run_poll(nhl=api, now=later, echo=lambda _: None)
    assert paths.count("/v1/gamecenter/2026020001/boxscore") == 1


def test_replay_rebuilds_the_table_from_the_raw_polls(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    key = "pregame-boxscore/2026-09-29/2026020001"
    store.put("nhl", f"{key}/20260929T110500Z", BOXSCORE, {"fetched_utc": MORNING.isoformat()})
    near = START - timedelta(minutes=15)
    flagged = with_goalies([goalie(CAR_STARTER, True)], [goalie(FLA_STARTER, True)])
    store.put("nhl", f"{key}/20260929T204500Z", flagged, {"fetched_utc": near.isoformat()})
    late = START + timedelta(minutes=2)
    store.put("nhl", f"{key}/20260929T210200Z", flagged, {"fetched_utc": late.isoformat()})
    lake = Lake(tmp_path / "lake")
    report = replay_pregame_goalies(store, lake)
    assert (report.responses, report.rows, report.after_start) == (3, 4, 1)
    table = lake.read("pregame_goalies")
    assert table["observed_utc"].unique().sort().to_list() == [MORNING, near]
    confirmed = table.filter(pl.col("starter_id").is_not_null())
    assert set(confirmed["starter_id"]) == {CAR_STARTER, FLA_STARTER}
    assert (confirmed["observed_utc"] == near).all()


def test_a_failed_page_does_not_stop_the_others(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.startswith("/v1/schedule/"):
            return httpx.Response(200, content=WEEK)
        if request.url.path == "/v1/gamecenter/2026020001/landing":
            return httpx.Response(404)
        if request.url.path.endswith("/boxscore"):
            return httpx.Response(200, content=BOXSCORE)
        return httpx.Response(200, content=b"{}")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=nhl_api.BASE_URL)
    calls = iter(range(10_000))
    api = NhlApi(
        store,
        client,
        min_interval_s=0.0,
        now=lambda: MORNING + timedelta(seconds=next(calls)),
        sleep=lambda _: None,
    )
    messages: list[str] = []
    report = run_poll(nhl=api, now=MORNING, echo=messages.append)
    assert report.failed == [2026020001]
    assert report.games == 5
    assert "/v1/gamecenter/2026020001/right-rail" in paths
    assert store.latest("nhl/pregame-right-rail/2026-09-29/2026020001") is not None
    assert store.latest("nhl/pregame-boxscore/2026-09-29/2026020001") is not None
    assert any("FLA at CAR landing" in message for message in messages)
