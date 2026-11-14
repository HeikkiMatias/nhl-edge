"""NHL standings by date (#422): the parser on one real response (2014-04-08, fetched
2026-11-14), the Standings schema, standings_as_of, the client's past-dates-only cache and
fetch_standings, all without the network. Other dates and the e flag are synthetic edits of the
fixture made in memory."""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import polars as pl
import pytest
from pandera.errors import SchemaError

from nhl_edge.ingest import nhl_api
from nhl_edge.ingest.games import result_public_utc
from nhl_edge.ingest.nhl_api import NhlApi, NotCachedError
from nhl_edge.ingest.standings import fetch_standings, parse_standings, standings_as_of
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import STANDINGS_PUBLIC_AFTER, Standings, dtypes

FIXTURE = Path(__file__).parent / "fixtures" / "nhl_api" / "standings_2014-04-08.json"
BODY = FIXTURE.read_bytes()
DAY = date(2014, 4, 8)
RAW_KEY = "nhl/standings/2014-04-08/20261114T120000Z"
ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def edited(change: Callable[[dict[str, Any]], object], body: bytes = BODY) -> bytes:
    data = json.loads(body)
    change(data)
    return json.dumps(data).encode()


def dated(day: date) -> bytes:
    """The fixture's standings relabelled as another date's: synthetic, for tests that need
    several dates."""

    def change(data: dict[str, Any]) -> None:
        for entry in data["standings"]:
            entry["date"] = day.isoformat()

    return edited(change)


def team(data: dict[str, Any], code: str) -> dict[str, Any]:
    return next(entry for entry in data["standings"] if entry["teamAbbrev"]["default"] == code)


def parse(body: bytes = BODY, day: date = DAY) -> pl.DataFrame:
    return parse_standings(body, day, f"nhl/standings/{day.isoformat()}/20261114T120000Z")


# The parser


def test_one_row_per_team_of_the_fixture() -> None:
    rows = parse()
    assert rows.height == 30
    assert rows["team"].is_unique().all()
    assert rows.select("season", "standings_date", "raw_key").unique().rows() == [
        (20132014, DAY, RAW_KEY)
    ]
    assert (rows["observed_utc"] == datetime(2014, 4, 9, 10, 0, tzinfo=UTC)).all()
    assert sorted(rows["conference"].unique()) == ["E", "W"]
    assert sorted(rows["division"].unique()) == ["A", "C", "M", "P"]


def test_the_clinch_flags_of_the_fixture() -> None:
    counts = dict(parse()["clinch_indicator"].value_counts().iter_rows())
    assert counts == {"x": 11, "y": 1, "z": 1, None: 17}
    # As the response lists them: 17 teams have no clinchIndicator at all.
    entries = json.loads(BODY)["standings"]
    assert sum("clinchIndicator" not in entry for entry in entries) == 17


def test_a_team_s_line() -> None:
    bos = parse().filter(pl.col("team") == "BOS").row(0, named=True)
    line = {key: bos[key] for key in ("conference", "division", "games_played", "points")}
    assert line == {"conference": "E", "division": "A", "games_played": 79, "points": 114}
    assert (bos["wins"], bos["losses"], bos["ot_losses"], bos["clinch_indicator"]) == (
        53,
        18,
        8,
        "z",
    )


def test_observed_the_morning_after_as_the_date_s_results() -> None:
    assert (parse()["observed_utc"] == result_public_utc(DAY)).all()
    morning = datetime(DAY.year, DAY.month, DAY.day, tzinfo=UTC) + STANDINGS_PUBLIC_AFTER
    assert result_public_utc(DAY) == morning


@pytest.mark.parametrize("flag", ["x", "y", "z", "p", "e"])
def test_every_clinch_flag_is_read(flag: str) -> None:
    # e is listed only from 2024-25: here a synthetic edit.
    body = edited(lambda data: team(data, "TOR").update(clinchIndicator=flag))
    rows = parse(body)
    assert rows.filter(pl.col("team") == "TOR")["clinch_indicator"].to_list() == [flag]


def test_a_null_clinch_flag_is_none() -> None:
    body = edited(lambda data: team(data, "BOS").update(clinchIndicator=None))
    assert parse(body).filter(pl.col("team") == "BOS")["clinch_indicator"].to_list() == [None]


@pytest.mark.parametrize("flag", ["w", "X", "", 1])
def test_an_unknown_clinch_flag_fails(flag: object) -> None:
    body = edited(lambda data: team(data, "BOS").update(clinchIndicator=flag))
    with pytest.raises(ValueError, match="clinchIndicator"):
        parse(body)


def test_standings_of_another_date_fail() -> None:
    with pytest.raises(ValueError, match="not of 2014-04-07"):
        parse(BODY, DAY - timedelta(days=1))
    one_off = edited(lambda data: team(data, "MTL").update(date="2014-04-07"))
    with pytest.raises(ValueError, match="MTL: the standings of '2014-04-07'"):
        parse(one_off)


def test_a_team_listed_twice_fails() -> None:
    def twice(data: dict[str, Any]) -> None:
        data["standings"].append(dict(team(data, "BOS")))

    with pytest.raises(ValueError, match="BOS listed more than once"):
        parse(edited(twice))


def remove(code: str, name: str) -> Callable[[dict[str, Any]], object]:
    return lambda data: team(data, code).pop(name)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"[]", "not a JSON object with a standings list"),
        (b"{}", "not a JSON object with a standings list"),
        (b'{"standings": {}}', "not a JSON object with a standings list"),
        (b'{"wildCardIndicator": true, "standings": []}', "list no team"),
        (b"not json", "Expecting value"),
        (edited(lambda data: data["standings"].append("BOS")), "a standings entry is a str"),
        (edited(remove("BOS", "teamAbbrev")), "no teamAbbrev.default"),
        (edited(remove("BOS", "gamesPlayed")), "gamesPlayed is None"),
        (edited(remove("BOS", "seasonId")), "seasonId is None"),
        (edited(remove("BOS", "divisionAbbrev")), "divisionAbbrev is None"),
        (edited(lambda data: team(data, "BOS").update(points="114")), "points is '114'"),
        (edited(lambda data: team(data, "BOS").update(wins=True)), "wins is True"),
    ],
    ids=[
        "list",
        "no standings",
        "standings not a list",
        "no team",
        "not json",
        "entry not an object",
        "no team code",
        "no games played",
        "no season",
        "no division",
        "points as text",
        "wins as a boolean",
    ],
)
def test_a_response_of_another_shape_fails(body: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse(body)


# The schema


def test_schema_accepts_the_parsed_rows() -> None:
    assert Standings.validate(parse()).height == 30
    assert list(parse().schema.items()) == list(dtypes(Standings).items())


def schema_rows(**changes: Any) -> pl.DataFrame:
    return parse().head(2).with_columns(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"points": pl.lit(-1, pl.Int16)},
        {"games_played": pl.lit(-1, pl.Int16)},
        {"losses": pl.col("games_played")},  # wins + losses + ot_losses over games played
        {"points": (2 * pl.col("games_played") + 1).cast(pl.Int16)},
        {"team": pl.lit("bos")},
        {"clinch_indicator": pl.lit("w")},
        {"conference": pl.lit("")},
        {"season": pl.lit(20132015, pl.Int32)},
        {"season": pl.lit(20152016, pl.Int32)},  # 2014-04-08 is not in 2015-16
        {"season": pl.Series([20132014, 20142015], dtype=pl.Int32)},  # two seasons on a date
        {"team": pl.lit("BOS")},  # twice on a date
        {"observed_utc": pl.col("observed_utc") - timedelta(microseconds=1)},
    ],
    ids=[
        "negative points",
        "negative games",
        "decisions over games",
        "points over two a game",
        "team code",
        "clinch flag",
        "no conference",
        "season",
        "date outside the season",
        "two seasons a date",
        "duplicate",
        "observed before the morning after",
    ],
)
def test_schema_rejects(changes: dict[str, Any]) -> None:
    with pytest.raises(SchemaError):
        Standings.validate(schema_rows(**changes))


def test_schema_needs_an_observed_time() -> None:
    rows = schema_rows(observed_utc=pl.lit(None, dtypes(Standings)["observed_utc"]))
    with pytest.raises(SchemaError):
        Standings.validate(rows)


# standings_as_of


def of_dates(*days: date) -> pl.DataFrame:
    return pl.concat([parse(dated(day), day) for day in days])


def at_ten_et(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 10, 0, tzinfo=ET).astimezone(UTC)


def asks(rows: list[tuple[str, datetime]], **extra: list[Any]) -> pl.DataFrame:
    frame = pl.DataFrame(
        {"team": [team for team, _ in rows], "as_of_utc": [at for _, at in rows]},
        schema={"team": pl.String, "as_of_utc": pl.Datetime("us", "UTC")},
    )
    return frame.with_columns(**{name: pl.Series(values) for name, values in extra.items()})


def test_a_game_on_d_reads_the_standings_of_the_day_before() -> None:
    before = DAY - timedelta(days=1)
    read = standings_as_of(of_dates(before, DAY), asks([("BOS", at_ten_et(DAY))]))
    assert read["standings_date"].to_list() == [before]
    assert read["observed_utc"].to_list() == [datetime(2014, 4, 8, 10, 0, tzinfo=UTC)]


def test_nothing_is_read_when_only_the_day_s_own_standings_exist() -> None:
    read = standings_as_of(of_dates(DAY), asks([("BOS", at_ten_et(DAY))]))
    assert read.height == 1
    assert read.row(0, named=True) == {"team": "BOS", "as_of_utc": at_ten_et(DAY)} | {
        column: None for column in Standings.to_schema().columns if column != "team"
    }


def test_a_row_observed_at_the_ask_s_very_time_is_not_read() -> None:
    days = [DAY - timedelta(days=2), DAY - timedelta(days=1)]
    observed = result_public_utc(days[1])
    read = standings_as_of(
        of_dates(*days),
        asks([("BOS", observed), ("BOS", observed + timedelta(microseconds=1))]),
    )
    assert read["standings_date"].to_list() == days


def test_the_asks_keep_their_order_and_columns() -> None:
    before = DAY - timedelta(days=1)
    rows = [("TOR", at_ten_et(DAY)), ("SEA", at_ten_et(DAY)), ("BOS", at_ten_et(before))]
    read = standings_as_of(of_dates(before, DAY), asks(rows, game_id=[3, 1, 2]))
    assert read.columns == ["team", "as_of_utc", "game_id"] + [
        column for column in Standings.to_schema().columns if column != "team"
    ]
    assert read["game_id"].to_list() == [3, 1, 2]
    # SEA had no team in 2013-14, and nothing of 04-06 or before is in the frame.
    assert read["standings_date"].to_list() == [before, None, None]
    tor = parse().filter(pl.col("team") == "TOR").row(0, named=True)
    assert read.row(0, named=True)["points"] == tor["points"]


def test_an_ask_in_another_time_zone_reads_the_same_instant() -> None:
    before = DAY - timedelta(days=1)
    eastern = asks([("BOS", at_ten_et(DAY))]).with_columns(
        pl.col("as_of_utc").dt.convert_time_zone("America/New_York")
    )
    read = standings_as_of(of_dates(before, DAY), eastern)
    assert read["standings_date"].to_list() == [before]


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (pl.DataFrame({"team": ["BOS"]}), "no as_of_utc"),
        (asks([("BOS", NOW)], season=[20132014]), "Standings columns of their own: season"),
        (pl.DataFrame({"team": ["BOS"], "as_of_utc": [datetime(2014, 4, 8, 14)]}), "time zone"),
        (
            pl.DataFrame(
                {"team": ["BOS"], "as_of_utc": [None]},
                schema={"team": pl.String, "as_of_utc": pl.Datetime("us", "UTC")},
            ),
            "an ask has no as_of_utc",
        ),
    ],
    ids=["no time", "shared column", "naive time", "null time"],
)
def test_asks_that_cannot_be_read_fail(frame: pl.DataFrame, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        standings_as_of(of_dates(DAY), frame)


# The client


class SetClock:
    """A UTC clock that stays where a test sets it."""

    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class Server:
    """Serves each date the fixture relabelled as that date's, or what a test sets for a date
    (bytes, or a status code), and records the paths asked for."""

    def __init__(self, answers: dict[date, bytes | int] | None = None) -> None:
        self.answers = answers or {}
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        day = date.fromisoformat(request.url.path.rsplit("/", 1)[1])
        answer = self.answers.get(day, dated(day))
        if isinstance(answer, int):
            return httpx.Response(answer)
        return httpx.Response(200, content=answer)


def api(
    store: RawStore, server: Any, clock: SetClock | None = None, *, offline: bool = False
) -> NhlApi:
    client = httpx.Client(transport=httpx.MockTransport(server), base_url=nhl_api.BASE_URL)
    return NhlApi(
        store,
        client,
        min_interval_s=0.0,
        backoff_s=(),
        now=clock or SetClock(),
        offline=offline,
        sleep=lambda _: None,
    )


def no_request(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no request expected, got {request.url}")


def test_standings_url_and_raw_key(tmp_path: Path) -> None:
    store, server = RawStore(tmp_path), Server({DAY: BODY})
    response = api(store, server).standings(DAY)
    assert server.paths == ["/v1/standings/2014-04-08"]
    assert response.raw_key == RAW_KEY
    assert not response.cached and response.fetched_utc == NOW
    assert store.get(RAW_KEY) == BODY
    assert store.meta(RAW_KEY)["url"] == "https://api-web.nhle.com/v1/standings/2014-04-08"


def test_a_past_date_is_fetched_once_and_reused(tmp_path: Path) -> None:
    store, server = RawStore(tmp_path), Server()
    first = api(store, server).standings(DAY)
    later = api(store, no_request, SetClock(NOW + timedelta(days=30))).standings(DAY)
    assert len(server.paths) == 1
    assert later.cached and later.raw_key == first.raw_key and later.body == first.body


def test_today_and_later_us_eastern_dates_are_refused_before_any_request(tmp_path: Path) -> None:
    # 03:00 UTC on 11-14 is still 11-13 in US Eastern time: 11-13 is today, 11-12 the latest past.
    clock = SetClock(datetime(2026, 11, 14, 3, 0, tzinfo=UTC))
    nhl = api(RawStore(tmp_path), no_request, clock)
    for day in (date(2026, 11, 13), date(2026, 11, 14), date(2027, 1, 1)):
        with pytest.raises(ValueError, match="not final"):
            nhl.standings(day)
    assert nhl.requests == 0
    server = Server()
    api(RawStore(tmp_path), server, clock).standings(date(2026, 11, 12))
    assert server.paths == ["/v1/standings/2026-11-12"]


def test_a_copy_fetched_in_the_night_after_the_date_is_fetched_again(tmp_path: Path) -> None:
    day = date(2026, 11, 13)
    store, server = RawStore(tmp_path), Server()
    clock = SetClock(datetime(2026, 11, 14, 5, 0, tzinfo=UTC))  # midnight US Eastern
    nhl = api(store, server, clock)
    night = nhl.standings(day)
    clock.at = datetime(2026, 11, 14, 9, 59, tzinfo=UTC)
    early = nhl.standings(day)
    assert not early.cached and early.raw_key != night.raw_key
    clock.at = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)  # the date's standings are settled
    settled = nhl.standings(day)
    clock.at = datetime(2026, 11, 20, tzinfo=UTC)
    later = nhl.standings(day)
    assert len(server.paths) == 3
    assert later.cached and later.raw_key == settled.raw_key
    assert later.raw_key.endswith("/20261114T100000Z")


def test_replay_reads_the_raw_cache_only(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    stored = api(store, Server()).standings(DAY)
    replay = api(store, no_request, offline=True)
    assert replay.standings(DAY).raw_key == stored.raw_key
    with pytest.raises(NotCachedError, match="nhl/standings/2014-04-07"):
        replay.standings(DAY - timedelta(days=1))


# fetch_standings


DAYS = [DAY - timedelta(days=1), DAY, DAY + timedelta(days=1)]


def test_fetch_parses_every_date_once_in_date_order(tmp_path: Path) -> None:
    server = Server()
    report = fetch_standings(api(RawStore(tmp_path), server), [DAYS[2], DAYS[0], DAYS[1], DAY])
    assert server.paths == [f"/v1/standings/{day.isoformat()}" for day in DAYS]
    assert (report.dates, report.fetched, report.reused, report.failed) == (3, 3, 0, [])
    assert report.standings.height == 90
    assert sorted(report.standings["standings_date"].unique()) == DAYS
    assert report.teams_per_date() == (30, 30)
    assert report.clinch_flags() == {"x": 33, "y": 3, "z": 3}


def test_a_rerun_reuses_every_copy(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    fetch_standings(api(store, Server()), DAYS)
    report = fetch_standings(api(store, no_request), DAYS)
    assert (report.dates, report.fetched, report.reused, report.failed) == (3, 0, 3, [])
    assert report.standings.height == 90


def test_failures_are_recorded_and_the_rest_go_on(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    server = Server({DAYS[0]: 404, DAYS[1]: b'{"standings": []}'})
    days = [*DAYS, NOW.date()]  # today is refused
    report = fetch_standings(api(store, server), days)
    assert (report.dates, report.fetched, report.reused) == (4, 2, 0)
    assert [problem.split(":")[0] for problem in report.failed] == [
        "2014-04-07",
        "2014-04-08",
        "2026-11-14",
    ]
    assert "NotFoundError" in report.failed[0]
    assert "ValueError" in report.failed[1] and "list no team" in report.failed[1]
    assert "not final" in report.failed[2]
    assert report.standings["standings_date"].unique().to_list() == [DAYS[2]]
    # The copy that does not parse is stored, for a fixed parser to replay.
    assert store.latest("nhl/standings/2014-04-08") is not None


def test_a_fetch_with_nothing_parsed_reports_no_teams(tmp_path: Path) -> None:
    report = fetch_standings(api(RawStore(tmp_path), Server({DAY: 404})), [DAY])
    assert report.standings.is_empty()
    assert report.teams_per_date() is None and report.clinch_flags() == {}
