"""NHL EDGE tracking snapshots (#424): the parser on one real copy of each team endpoint (MTL,
2026-27 regular season, fetched 2026-11-12), the EdgeTeamMetrics schema, the team ids and the
daily snapshot, all without the network."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
from pandera.errors import SchemaError

from nhl_edge.ingest import edge_tracking as et
from nhl_edge.ingest import nhl_api
from nhl_edge.ingest.nhl_api import NhlApi
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import EDGE_ENDPOINTS, EdgeTeamMetrics, dtypes

EDGE = Path(__file__).parent / "fixtures" / "nhl_api" / "edge"
SEASON = 20262027
MTL = 8
FETCHED = datetime(2026, 11, 12, 9, 5, 30, tzinfo=UTC)
# Figures in each fixture: every number but those of ids, nulls and the player highlight blocks.
FIGURES = {
    "team-detail": 109,
    "team-zone-time-details": 40,
    "team-skating-speed-detail": 42,
    "team-skating-distance-detail": 357,
    "team-shot-speed-detail": 60,
    "team-shot-location-detail": 200,
}


def body(endpoint: str) -> bytes:
    return (EDGE / f"{endpoint}_8_{SEASON}_2.json").read_bytes()


def parse(endpoint: str, data: bytes | None = None, **keys: Any) -> pl.DataFrame:
    keys = {"team": "MTL", "team_id": MTL, "season": SEASON, "game_type": 2} | keys
    return et.parse_edge_team(
        endpoint,
        body(endpoint) if data is None else data,
        observed_utc=FETCHED,
        raw_key=f"nhl/edge-{endpoint}/{SEASON}/8/2/20261112T090530Z",
        **keys,
    )


def values(rows: pl.DataFrame) -> dict[str, float]:
    return dict(zip(rows["metric"], rows["value"], strict=True))


def edited(endpoint: str, change: Any) -> bytes:
    data = json.loads(body(endpoint))
    change(data)
    return json.dumps(data).encode()


# The parser


@pytest.mark.parametrize("endpoint", EDGE_ENDPOINTS)
def test_every_figure_of_each_endpoint_is_one_row(endpoint: str) -> None:
    rows = parse(endpoint)
    assert rows.height == FIGURES[endpoint]
    assert rows["metric"].is_unique().all()
    assert rows.select("team", "team_id", "season", "game_type", "endpoint").unique().rows() == [
        ("MTL", MTL, SEASON, 2, endpoint)
    ]
    assert (rows["observed_utc"] == FETCHED).all()
    assert rows["raw_key"].unique().to_list() == [
        f"nhl/edge-{endpoint}/{SEASON}/8/2/20261112T090530Z"
    ]


def test_list_items_are_named_by_the_fields_that_identify_them() -> None:
    zone = values(parse("team-zone-time-details"))
    assert zone["zoneTimeDetails[strengthCode=all].offensiveZonePctg"] == 0.4154762
    assert zone["zoneTimeDetails[strengthCode=es].neutralZoneRank"] == 32
    assert zone["zoneTimeDetails[strengthCode=pk].defensiveZoneLeagueAvg"] == 0.6544967
    assert zone["shotDifferential.shotAttemptDifferential"] == -0.388889
    distance = values(parse("team-skating-distance-detail"))
    total = "skatingDistanceDetails[strengthCode=all,positionCode=all].distanceTotal.metric"
    assert distance[total] == 1298.8192
    # The last ten games' distances, each named by its date.
    assert distance["skatingDistanceLast10[gameDate=2026-11-10].toiAll"] == 17661
    location = values(parse("team-shot-location-detail"))
    assert location["shotLocationTotals[locationCode=all,position=all].sog"] == 498
    assert location["shotLocationDetails[area=Behind the Net].sog"] == 8
    speed = values(parse("team-shot-speed-detail"))
    assert speed["shotSpeedDetails[position=all].avgShotSpeed.imperial"] == 59.8009


def test_team_detail_keeps_record_ranks_and_league_averages() -> None:
    detail = values(parse("team-detail"))
    assert detail["team.gamesPlayed"] == 18
    assert detail["team.points"] == 26
    assert detail["skatingSpeed.burstsOver20.rank"] == 6
    assert detail["skatingSpeed.burstsOver20.leagueAvg.value"] == 421
    assert detail["zoneTimeDetails.offensiveZoneEvPctg"] == 0.4183988
    assert detail["sogSummary[locationCode=high].goals"] == 28


def test_text_ids_nulls_and_player_highlights_give_no_row() -> None:
    metrics = pl.concat([parse(endpoint) for endpoint in EDGE_ENDPOINTS])["metric"]
    for skipped in ("overlay", "topSkatingSpeeds", "hardestShots", "seasonsWithEdgeStats"):
        assert not metrics.str.contains(skipped, literal=True).any(), skipped
    leaves = metrics.str.extract(r"([^.]+)$")
    for text in ("id", "abbrev", "slug", "default", "light", "gameCenterLink", "isHomeTeam"):
        assert text not in leaves.to_list(), text
    # The API gives a null shooting percentage where there was no goal: no row, not a zero.
    location = values(parse("team-shot-location-detail"))
    assert "shotLocationDetails[area=Behind the Net].goals" in location
    assert "shotLocationDetails[area=Behind the Net].shootingPctg" not in location


def test_figures_walks_numbers_only() -> None:
    data = {
        "a": {"b": 1, "c": "text", "d": None, "e": True, "id": 7},
        "f": [{"area": "Slot", "g": 2.5}],
    }
    assert list(et.figures(data)) == [("a.b", 1.0), ("f[area=Slot].g", 2.5)]


@pytest.mark.parametrize(
    ("endpoint", "change", "message"),
    [
        ("team-zone-time-details", lambda d: d.pop("shotDifferential"), "has no shotDifferential"),
        (
            "team-detail",
            lambda d: d["team"].update(abbrev="TOR", id=10),
            "of TOR \\(10\\), not MTL",
        ),
        # A list item no field names could only be named by its position.
        (
            "team-shot-location-detail",
            lambda d: d["shotLocationDetails"][0].pop("area"),
            "none of the fields that name one",
        ),
        (
            "team-zone-time-details",
            lambda d: d.update(zoneTimeDetails=[], shotDifferential={}),
            "no figure",
        ),
    ],
)
def test_a_changed_response_fails_its_parse(endpoint: str, change: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse(endpoint, edited(endpoint, change))


def test_a_page_that_is_not_json_fails_its_parse() -> None:
    with pytest.raises(ValueError):
        parse("team-detail", b"<html>Error 404 Not Found</html>")


def test_blocks_cover_every_endpoint() -> None:
    assert set(et.BLOCKS) == set(EDGE_ENDPOINTS)


def test_game_types_listed_for_the_season() -> None:
    detail = body("team-detail")
    assert et.edge_game_types(detail, SEASON) == (2,)
    assert et.edge_game_types(detail, 20252026) == (2, 3)
    assert et.edge_game_types(detail, 20272028) == ()


# The schema


def schema_rows(**changes: Any) -> pl.DataFrame:
    return parse("team-zone-time-details").head(2).with_columns(**changes)


def test_schema_accepts_the_parsed_rows() -> None:
    rows = pl.concat([parse(endpoint) for endpoint in EDGE_ENDPOINTS])
    assert EdgeTeamMetrics.validate(rows).height == sum(FIGURES.values())


@pytest.mark.parametrize(
    "changes",
    [
        {"game_type": pl.lit(1, pl.Int8)},
        {"endpoint": pl.lit("team-landing")},
        {"season": pl.lit(20262028, pl.Int32)},
        {"team": pl.lit("mtl")},
        {"value": pl.lit(float("nan"))},
        {"metric": pl.lit("zoneTimeDetails[strengthCode=all].offensiveZonePctg")},  # twice
    ],
    ids=["game type", "endpoint", "season", "team code", "nan", "duplicate"],
)
def test_schema_rejects(changes: dict[str, pl.Expr]) -> None:
    with pytest.raises(SchemaError):
        EdgeTeamMetrics.validate(schema_rows(**changes))


def test_schema_needs_an_observed_time() -> None:
    rows = schema_rows().with_columns(
        observed_utc=pl.lit(None, dtypes(EdgeTeamMetrics)["observed_utc"])
    )
    with pytest.raises(SchemaError):
        EdgeTeamMetrics.validate(rows)


# Team ids and the daily snapshot


class Clock:
    def __init__(self, at: datetime = FETCHED) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class Server:
    """Serves each EDGE endpoint its fixture, or what a test sets per endpoint and game type
    (bytes, or a status code), and records the paths asked for."""

    def __init__(self, **answers: dict[int, bytes | int]) -> None:
        self.answers = answers
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        _, _, _, endpoint, _, _, game_type = request.url.path.split("/")
        answer = self.answers.get(endpoint.replace("-", "_"), {}).get(int(game_type))
        if isinstance(answer, int):
            return httpx.Response(answer)
        return httpx.Response(200, content=answer if answer is not None else body(endpoint))


def api(store: RawStore, server: Any, clock: Clock | None = None) -> NhlApi:
    client = httpx.Client(transport=httpx.MockTransport(server), base_url=nhl_api.BASE_URL)
    return NhlApi(store, client, min_interval_s=0.0, now=clock or Clock(), sleep=lambda _: None)


def no_request(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no request expected, got {request.url}")


def test_snapshot_stores_and_parses_every_endpoint(tmp_path: Path) -> None:
    store, server = RawStore(tmp_path), Server()
    report = et.snapshot(api(store, server), {"MTL": MTL}, SEASON)
    assert server.paths == [f"/v1/edge/{endpoint}/8/{SEASON}/2" for endpoint in EDGE_ENDPOINTS]
    assert (report.teams, report.playoff_teams, report.responses) == (1, 0, 6)
    assert (report.fetched, report.reused, report.failed, report.not_listed) == (6, 0, [], [])
    assert report.figures == sum(FIGURES.values())
    for endpoint in EDGE_ENDPOINTS:
        assert store.latest(f"nhl/edge-{endpoint}/{SEASON}/8/2") is not None


def test_a_rerun_the_same_day_reuses_every_copy(tmp_path: Path) -> None:
    store, clock = RawStore(tmp_path), Clock()
    et.snapshot(api(store, Server(), clock), {"MTL": MTL}, SEASON)
    clock.at = FETCHED + timedelta(hours=6)
    report = et.snapshot(api(store, no_request, clock), {"MTL": MTL}, SEASON)
    assert (report.responses, report.fetched, report.reused) == (6, 0, 6)
    assert report.figures == sum(FIGURES.values())


def test_playoff_figures_once_team_detail_lists_them(tmp_path: Path) -> None:
    def playoffs(data: dict[str, Any]) -> None:
        data["seasonsWithEdgeStats"][-1]["gameTypes"] = [2, 3]

    server = Server(team_detail={2: edited("team-detail", playoffs)})
    report = et.snapshot(api(RawStore(tmp_path), server), {"MTL": MTL}, SEASON)
    assert server.paths[6:] == [f"/v1/edge/{endpoint}/8/{SEASON}/3" for endpoint in EDGE_ENDPOINTS]
    assert (report.teams, report.playoff_teams, report.responses, report.failed) == (1, 1, 12, [])


def test_a_team_without_edge_figures_for_the_season_is_left_at_its_team_detail(
    tmp_path: Path,
) -> None:
    def before_the_season(data: dict[str, Any]) -> None:
        data["seasonsWithEdgeStats"].pop()

    server = Server(team_detail={2: edited("team-detail", before_the_season)})
    report = et.snapshot(api(RawStore(tmp_path), server), {"MTL": MTL}, SEASON)
    assert len(server.paths) == 1
    assert (report.teams, report.responses, report.not_listed, report.failed) == (0, 1, ["MTL"], [])


def test_a_failed_endpoint_is_reported_after_the_rest_are_stored(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    server = Server(team_shot_speed_detail={2: 404}, team_zone_time_details={2: b"{}"})
    report = et.snapshot(api(store, server), {"MTL": MTL}, SEASON)
    assert len(server.paths) == 6
    assert report.responses == 5
    assert report.figures == sum(FIGURES.values()) - sum(
        FIGURES[endpoint] for endpoint in ("team-zone-time-details", "team-shot-speed-detail")
    )
    assert [problem.split(":")[0] for problem in report.failed] == [
        "MTL team-zone-time-details 2",
        "MTL team-shot-speed-detail 2",
    ]
    # The copy that no longer parses is stored all the same, for a fixed parser to replay.
    zone = store.latest(f"nhl/edge-team-zone-time-details/{SEASON}/8/2")
    assert zone is not None and store.get(zone) == b"{}"


def boxscore(game_id: int, home: tuple[str, int], away: tuple[str, int]) -> bytes:
    return json.dumps(
        {
            "id": game_id,
            "homeTeam": {"abbrev": home[0], "id": home[1]},
            "awayTeam": {"abbrev": away[0], "id": away[1]},
        }
    ).encode()


GAMES = [
    # game_id, season, date, home, away, home id, away id. Utah had id 59 in 2024-25, then 68.
    (2024021300, 20242025, date(2025, 4, 16), "UTA", "MTL", 59, MTL),
    (2026020240, SEASON, date(2026, 11, 4), "MTL", "UTA", MTL, 68),
    (2026020251, SEASON, date(2026, 11, 5), "TOR", "UTA", 10, 68),
    (2026020287, SEASON, date(2026, 11, 10), "MTL", "MIN", MTL, 30),
]


def store_games(store: RawStore) -> pl.DataFrame:
    rows = []
    for game_id, season, day, home, away, home_id, away_id in GAMES:
        start = datetime(day.year, day.month, day.day, 23, tzinfo=UTC)
        fetched = start + timedelta(hours=10)
        body = boxscore(game_id, (home, home_id), (away, away_id))
        meta = {"fetched_utc": fetched.isoformat()}
        store.put("nhl", f"boxscore/{season}/{game_id}/{fetched:%Y%m%dT%H%M%SZ}", body, meta)
        rows.append(
            {"game_id": game_id, "season": season, "game_date": day, "start_utc": start}
            | {"home": home, "away": away}
        )
    return pl.DataFrame(rows)


def test_team_ids_come_from_the_season_s_latest_boxscores(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    games = store_games(store)
    found = et.team_ids(api(store, no_request), games, SEASON)
    assert found == {"MIN": 30, "MTL": MTL, "TOR": 10, "UTA": 68}
    assert et.team_ids(api(store, no_request), games, SEASON, ["UTA"]) == {"UTA": 68}
    # A team without a final game of the season has no id yet.
    assert et.team_ids(api(store, no_request), games, SEASON, ["MTL", "SEA"]) == {"MTL": MTL}
    assert et.team_ids(api(store, no_request), games, 20242025) == {"MTL": MTL, "UTA": 59}
    assert et.team_ids(api(store, no_request), games, 20272028) == {}


def test_team_ids_read_no_more_boxscores_than_needed(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    games = store_games(store)
    one = api(store, no_request)
    et.team_ids(one, games, SEASON, ["MTL"])
    assert one.cache_hits == 1
    # The latest game gives MTL and MIN, the one before it TOR and UTA: 2026020240 isn't read.
    every = api(store, no_request)
    et.team_ids(every, games, SEASON)
    assert every.cache_hits == 2
