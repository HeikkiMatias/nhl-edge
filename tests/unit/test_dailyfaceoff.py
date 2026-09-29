from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pandera.errors
import polars as pl
import pytest

from nhl_edge.ingest import dailyfaceoff
from nhl_edge.ingest.dailyfaceoff import (
    DailyFaceoff,
    parse_starting_goalies,
    replay_starting_goalies,
    run_poll,
    season_of,
)
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import DailyFaceoffGoalies
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
