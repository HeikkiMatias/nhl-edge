from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pandera.errors
import polars as pl
import pytest

from nhl_edge.ingest import dailyfaceoff
from nhl_edge.ingest.dailyfaceoff import (
    DailyFaceoff,
    parse_line_combinations,
    parse_starting_goalies,
    replay_line_combinations,
    replay_starting_goalies,
    run_lines_poll,
    run_poll,
    season_of,
)
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import DailyFaceoffGoalies, DailyFaceoffLines
from nhl_edge.lake.tables import Lake

# FLA at CAR (no reports yet) and VAN at EDM (Jarry confirmed the day before), trimmed from the
# 2026-09-29 page fetched at about 12:10 UTC.
PAGE = (
    Path(__file__).parent / "fixtures" / "dailyfaceoff" / "starting-goalies_2026-09-29_trimmed.html"
).read_bytes()
FETCHED = datetime(2026, 9, 29, 12, 10, tzinfo=UTC)
CAR_START = datetime(2026, 9, 29, 21, 0, tzinfo=UTC)
EDM_START = datetime(2026, 9, 30, 2, 0, tzinfo=UTC)


def test_parses_each_team_with_its_status() -> None:
    frame = parse_starting_goalies(PAGE, FETCHED, "dailyfaceoff/starting-goalies/x")
    assert frame.select("team", "is_home", "goalie_name", "status").rows() == [
        ("CAR", True, "Brandon Bussi", None),
        ("FLA", False, "Jacob Markstrom", None),
        ("EDM", True, "Tristan Jarry", "Confirmed"),
        ("VAN", False, "Kevin Lankinen", None),
    ]
    jarry = frame.filter(pl.col("team") == "EDM").row(0, named=True)
    assert jarry["reported_utc"] == datetime(2026, 9, 28, 17, 42, 37, 590000, tzinfo=UTC)
    assert jarry["source_url"].startswith("https://x.com/")
    assert jarry["start_utc"] == EDM_START
    assert (frame["observed_utc"] == FETCHED).all()
    assert (frame["game_date"] == date(2026, 9, 29)).all()
    assert (frame["season"] == 20262027).all()


def test_games_started_at_the_fetch_are_left_out() -> None:
    frame = parse_starting_goalies(PAGE, CAR_START, "k")
    assert set(frame["team"]) == {"EDM", "VAN"}
    assert parse_starting_goalies(PAGE, EDM_START, "k").is_empty()


def test_schema_rejects_a_report_after_the_fetch() -> None:
    frame = parse_starting_goalies(PAGE, FETCHED, "k").with_columns(
        reported_utc=pl.lit(FETCHED + timedelta(hours=1)).dt.cast_time_unit("us")
    )
    with pytest.raises(pandera.errors.SchemaError):
        DailyFaceoffGoalies.validate(frame)


def test_season_turns_over_in_july() -> None:
    assert season_of(date(2026, 9, 29)) == 20262027
    assert season_of(date(2027, 4, 10)) == 20262027
    assert season_of(date(2027, 7, 1)) == 20272028


def fake(store: RawStore, status: int = 200) -> tuple[DailyFaceoff, list[str]]:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(status, content=PAGE)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=dailyfaceoff.BASE_URL)
    return DailyFaceoff(store, client, now=lambda: FETCHED, sleep=lambda _: None), paths


def test_poll_stores_the_page_raw_and_replay_rebuilds_the_table(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    dfo, paths = fake(store)
    messages: list[str] = []
    assert run_poll(dfo=dfo, days={date(2026, 9, 29)}, echo=messages.append) == []
    assert paths == ["/starting-goalies/2026-09-29"]
    raw_key = store.latest("dailyfaceoff/starting-goalies/2026-09-29")
    assert raw_key is not None and store.get(raw_key) == PAGE
    assert "4 teams, 1 with a status, 1 confirmed" in messages[0]
    lake = Lake(tmp_path / "lake")
    report = replay_starting_goalies(store, lake)
    assert (report.pages, report.rows) == (1, 4)
    assert lake.read("dailyfaceoff_goalies").height == 4


def test_a_failed_page_is_reported(tmp_path: Path) -> None:
    dfo, _ = fake(RawStore(tmp_path / "raw"), status=503)
    messages: list[str] = []
    failed = run_poll(dfo=dfo, days={date(2026, 9, 29)}, echo=messages.append)
    assert failed == [date(2026, 9, 29)]
    assert "503" in messages[0]


def test_never_calls_the_api_path() -> None:
    # robots.txt disallows /api/; only the public page is fetched.
    assert not dailyfaceoff.PAGES.startswith("api")
    assert dailyfaceoff.BASE_URL == "https://www.dailyfaceoff.com"


# Washington's line-combinations page fetched on 2026-10-08 at 10:01 UTC, trimmed to six players:
# two first-line forwards, a defenceman, a goalie, a power-play entry, and the two injured.
LINES = (
    Path(__file__).parent
    / "fixtures"
    / "dailyfaceoff"
    / "line-combinations_WSH_2026-10-08_trimmed.html"
).read_bytes()
LINES_FETCHED = datetime(2026, 10, 8, 10, 1, 10, tzinfo=UTC)
GAME_DAY = date(2026, 10, 8)


def test_parses_each_player_in_each_group_with_his_status() -> None:
    frame = parse_line_combinations(LINES, LINES_FETCHED, "k", GAME_DAY)
    assert frame.select("player_name", "group", "injury_status").rows() == [
        ("Alex Tuch", "f1", None),
        ("Pierre-Luc Dubois", "f1", None),
        ("Jakob Chychrun", "d1", None),
        ("Logan Thompson", "g", None),
        ("Tom Wilson", "pp1", None),
        ("Rasmus Sandin", "ir", "out"),
        ("Matt Roy", "ir", "dtd"),
    ]
    roy = frame.filter(pl.col("player_name") == "Matt Roy").row(0, named=True)
    assert roy["news_utc"] == datetime(2026, 10, 7, 15, 19, 57, 988000, tzinfo=UTC)
    assert roy["game_time_decision"] is False
    assert (frame["team"] == "WSH").all() and (frame["season"] == 20262027).all()
    assert (
        frame["lines_updated_utc"] == datetime(2026, 10, 6, 15, 36, 43, 17000, tzinfo=UTC)
    ).all()
    assert (frame["lines_source"] == "Sammi Silber").all()
    assert (frame["observed_utc"] == LINES_FETCHED).all()


def test_schema_rejects_lines_updated_after_the_fetch() -> None:
    frame = parse_line_combinations(LINES, LINES_FETCHED, "k", GAME_DAY).with_columns(
        lines_updated_utc=pl.lit(LINES_FETCHED + timedelta(hours=1)).dt.cast_time_unit("us")
    )
    with pytest.raises(pandera.errors.SchemaError):
        DailyFaceoffLines.validate(frame)


def fake_lines(
    store: RawStore, page: bytes = LINES, clock: list[datetime] | None = None
) -> tuple[DailyFaceoff, list[str]]:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, content=page)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=dailyfaceoff.BASE_URL)
    times = iter(clock) if clock is not None else None
    now = (lambda: next(times)) if times is not None else (lambda: LINES_FETCHED)
    return DailyFaceoff(store, client, now=now, sleep=lambda _: None), paths


def test_lines_poll_stores_each_team_page_raw_and_replay_rebuilds_the_table(
    tmp_path: Path,
) -> None:
    store = RawStore(tmp_path / "raw")
    dfo, paths = fake_lines(store)
    messages: list[str] = []
    assert run_lines_poll(dfo=dfo, teams={("WSH", GAME_DAY)}, echo=messages.append) == []
    assert paths == ["/teams/washington-capitals/line-combinations"]
    raw_key = store.latest("dailyfaceoff/line-combinations/2026-10-08/WSH")
    assert raw_key is not None and store.get(raw_key) == LINES
    assert "1 teams, 2 injured players, 0 game-time decisions; 0 failed" in messages[0]
    lake = Lake(tmp_path / "lake")
    report = replay_line_combinations(store, lake)
    assert (report.pages, report.rows, report.dates) == (1, 7, [GAME_DAY])
    assert lake.read("dailyfaceoff_lines").height == 7


def test_another_teams_page_is_a_failure(tmp_path: Path) -> None:
    dfo, _ = fake_lines(RawStore(tmp_path / "raw"))
    messages: list[str] = []
    assert run_lines_poll(dfo=dfo, teams={("PIT", GAME_DAY)}, echo=messages.append) == ["PIT"]
    assert "not PIT's" in messages[0]


def test_the_lines_poll_stops_when_its_time_runs_out(tmp_path: Path) -> None:
    # Each read of the clock moves it a minute on: the budget runs out before the third team.
    clock = [LINES_FETCHED + timedelta(minutes=i) for i in range(20)]
    dfo, paths = fake_lines(RawStore(tmp_path / "raw"), clock=clock)
    messages: list[str] = []
    teams = {("WSH", GAME_DAY), ("PIT", GAME_DAY), ("BOS", GAME_DAY), ("TOR", GAME_DAY)}
    run_lines_poll(dfo=dfo, teams=teams, echo=messages.append)
    assert len(paths) < len(teams)
    assert "skipped after 2 minutes" in messages[-1]


def test_every_team_has_a_page() -> None:
    assert len(dailyfaceoff.TEAM_SLUGS) == 32
    assert dailyfaceoff.TEAM_SLUGS["WSH"] == "washington-capitals"
    assert not dailyfaceoff.LINES.startswith("api")


def test_a_page_that_does_not_parse_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    broken = LINES.replace(b'"name":"Matt Roy"', b'"name":null')
    dfo, _ = fake_lines(RawStore(tmp_path / "raw"), page=broken)
    messages: list[str] = []
    assert run_lines_poll(dfo=dfo, teams={("WSH", GAME_DAY)}, echo=messages.append) == ["WSH"]
