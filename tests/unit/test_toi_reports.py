import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import httpx
import polars as pl
import pytest
from feed_fixtures import feed, feed_game, games_row, parsed_feeds, raw_key
from toi_fixtures import (
    ALLEN,
    EMPTY_CHART,
    GEEKIE,
    HAULA,
    LAUKO,
    PALAT,
    STAMP,
    SWAYMAN,
    TOI_GAME,
    TOI_SEASON,
    player_id,
    synthetic_play_by_play,
    toi_boxscore,
    toi_feeds,
    toi_game,
    toi_games_row,
    toi_key,
    toi_lineups,
    toi_page,
    toi_reports,
)

from nhl_edge.ingest import nhl_api
from nhl_edge.ingest.games import result_public_utc
from nhl_edge.ingest.nhl_api import BROWSER_USER_AGENT, NhlApi, parse_utc
from nhl_edge.ingest.nhl_ingest import Ingest, parse_feeds
from nhl_edge.ingest.shifts import ShiftDrops
from nhl_edge.ingest.toi_reports import (
    ALLOWED_GAMES,
    TITLES,
    CachedReports,
    ReportPlayer,
    TimeOnIceReport,
    chartless_games,
    fetch_reports,
    malformed_rows,
    parse_reports,
    read_report,
    report_rows,
)
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.tables import FEED_TABLES, Lake

Handler = Callable[[httpx.Request], httpx.Response]
# Shifts in the trimmed reports: Swayman, Geekie and Lauko of BOS; Palat, Allen and Haula of NJD.
SHIFTS = {("BOS", SWAYMAN): 5, ("BOS", GEEKIE): 23, ("BOS", LAUKO): 17}
SHIFTS |= {("NJD", PALAT): 4, ("NJD", ALLEN): 4, ("NJD", HAULA): 23}
FETCHED = parse_utc(f"{STAMP[:4]}-{STAMP[4:6]}-{STAMP[6:8]}T12:00:00Z")


def store_reports(store: RawStore, season: int = TOI_SEASON, game_id: int = TOI_GAME) -> None:
    """Both trimmed reports in the raw cache under the game's keys, as nhl toi-reports leaves
    them."""
    for side in ("home", "visitor"):
        meta = {"url": "u", "status": 200, "fetched_utc": FETCHED.isoformat(), "attempts": 1}
        store.put("nhl", f"toi-{side}/{season}/{game_id}/{STAMP}", toi_page(side), meta)


def shift_of(shifts: pl.DataFrame, pid: int, number: int) -> tuple[int, int, int]:
    rows = shifts.filter(pl.col("player_id") == pid, pl.col("shift_number") == number)
    return rows.select("period", "start_s", "end_s").row(0)


# Reading a page


def test_a_report_lists_each_players_shift_rows() -> None:
    home = read_report(toi_page("home"))
    assert (home.title, home.game_number, home.team_name) == (
        "Time On Ice Report Home Team",
        1291,
        "BOSTON BRUINS",
    )
    assert [(p.sweater_number, p.heading, len(p.rows)) for p in home.players] == [
        (SWAYMAN, "SWAYMAN, JEREMY", 5),
        (GEEKIE, "GEEKIE, MORGAN", 23),
        (LAUKO, "LAUKO, JAKUB", 17),
    ]
    # Only shift rows: the column headings and each player's summary by period are left out.
    rows = [cells for player in home.players for cells in player.rows]
    assert all(len(cells) == 6 and cells[0].isdigit() and " / " in cells[2] for cells in rows)
    assert home.shift_count == len(rows) and malformed_rows(home) == 0
    visitor = read_report(toi_page("visitor"))
    assert (visitor.title, visitor.team_name) == (TITLES["visitor"], "NEW JERSEY DEVILS")
    assert [p.sweater_number for p in visitor.players] == [PALAT, ALLEN, HAULA]


# Shifts from the reports


def test_shift_times_are_elapsed_game_seconds() -> None:
    lineups = toi_lineups()
    built = parse_reports(toi_reports(), toi_game(), lineups)
    assert (built.drops, built.problems) == (ShiftDrops(), ())
    shifts = built.shifts
    assert shifts.height == sum(SHIFTS.values())
    swayman, geekie = player_id(lineups, "BOS", SWAYMAN), player_id(lineups, "BOS", GEEKIE)
    # Swayman's first shift: "0:00 / 20:00" to "8:41 / 11:19" of the first period.
    assert shift_of(shifts, swayman, 1) == (1, 0, 521)
    # Geekie's ninth starts the second period at 0:00 and ends at 0:29.
    assert shift_of(shifts, geekie, 9) == (2, 1200, 1229)
    # Overtime is period 4, from 3600: Swayman's last shift runs from 0:00 to 1:30 of it.
    assert shift_of(shifts, swayman, 5) == (4, 3600, 3690)
    assert shift_of(shifts, player_id(lineups, "NJD", HAULA), 23) == (4, 3680, 3690)
    overtime = shifts.filter(pl.col("period") == 4)
    assert overtime["start_s"].min() == 3600
    assert overtime.select(pl.col("end_s").max() <= 3900).item()


def test_sweater_numbers_map_to_the_boxscore_player_ids() -> None:
    lineups = toi_lineups()
    shifts = parse_reports(toi_reports(), toi_game(), lineups).shifts
    counts = shifts.group_by("team", "player_id").len()
    expected = {
        (team, player_id(lineups, team, number)): count for (team, number), count in SHIFTS.items()
    }
    assert {(t, p): n for t, p, n in counts.iter_rows()} == expected
    # Each row keeps its own report's raw key, and is public with the game's other feeds.
    assert set(shifts.filter(pl.col("team") == "BOS")["raw_key"]) == {toi_key("toi-home")}
    assert set(shifts.filter(pl.col("team") == "NJD")["raw_key"]) == {toi_key("toi-visitor")}
    assert (shifts["observed_utc"] == result_public_utc(toi_game().game_date)).all()


def test_a_sweater_number_not_in_the_boxscore_is_a_problem_not_a_silent_drop() -> None:
    lineups = toi_lineups()
    lauko = (pl.col("team") == "BOS") & (pl.col("sweater_number") == LAUKO)
    built = parse_reports(toi_reports(), toi_game(), lineups.filter(~lauko))
    assert built.problems == (
        "home report: 'LAUKO, JAKUB' has a number not in BOS's boxscore; "
        "17 shifts counted as bad rows",
    )
    assert built.drops == ShiftDrops(bad=17)
    assert built.shifts.height == sum(SHIFTS.values()) - 17


def test_a_sweater_number_given_twice_in_the_boxscore_is_a_problem() -> None:
    lineups = toi_lineups()
    haula = lineups.filter(pl.col("team") == "NJD", pl.col("sweater_number") == HAULA)
    twice = pl.concat([lineups, haula.with_columns(player_id=pl.lit(1, pl.Int64))])
    built = parse_reports(toi_reports(), toi_game(), twice)
    [problem] = built.problems
    assert problem.startswith("visitor report: 'HAULA, ERIK' is the number of 2 players")
    assert built.drops == ShiftDrops(bad=23)


def test_a_heading_without_a_sweater_number_is_a_problem() -> None:
    page = toi_page("visitor").replace(b">18 PALAT, ONDREJ<", b">PALAT, ONDREJ<")
    reports = {**toi_reports(), "visitor": (page, toi_key("toi-visitor"))}
    built = parse_reports(reports, toi_game(), toi_lineups())
    assert built.problems == (
        "visitor report: 'PALAT, ONDREJ' has no sweater number; 4 shifts counted as bad rows",
    )
    assert built.drops == ShiftDrops(bad=4)


def test_unreadable_rows_are_bad_and_repeats_are_dropped() -> None:
    rows = (
        ("1", "1", "0:00 / 20:00", "0:40 / 19:20", "00:40", ""),  # kept
        ("2", "1", "1:00 / 19:00", "1:30 / 18:00", "00:30", ""),  # end halves add to 19:30
        ("3", "SO", "0:00 / 5:00", "0:10 / 4:50", "00:10", ""),  # no such period
        ("4", "2", "1:00 / 19:00", "1:00 / 19:00", "00:00", ""),  # zero length: dropped
        ("5", "2", "2:00 - 18:00", "2:30 / 17:30", "00:30", ""),  # not a clock pair
        ("x", "2", "3:00 / 17:00", "3:30 / 16:30", "00:30", ""),  # no shift number
        ("7", "1", "0:00 / 20:00", "0:40 / 19:20", "00:40", ""),  # shift 1 again: dropped
        ("8", "3", "19:50 / 0:10", "20:00 / 0:00", "00:10", ""),  # kept, to the period's end
    )
    report = TimeOnIceReport(
        TITLES["home"], 1291, "BOSTON BRUINS", (ReportPlayer(SWAYMAN, "SWAYMAN, JEREMY", rows),)
    )
    kept, drops, problems = report_rows(report, "home", toi_game(), toi_lineups(), "k")
    assert [(r["shift_number"], r["start_s"], r["end_s"]) for r in kept] == [
        (1, 0, 40),
        (8, 3590, 3600),
    ]
    assert (drops, problems) == (ShiftDrops(dropped=2, bad=4), [])
    assert malformed_rows(report) == 4


def test_a_page_of_the_other_side_or_another_game_is_an_error() -> None:
    reports = toi_reports()
    swapped = {"home": reports["visitor"], "visitor": reports["home"]}
    with pytest.raises(ValueError, match="titled 'Time On Ice Report Away Team'"):
        parse_reports(swapped, toi_game(), toi_lineups())
    page = toi_page("home").replace(b">Game 1291<", b">Game 1290<")
    with pytest.raises(ValueError, match="for game 1290"):
        parse_reports({**reports, "home": (page, "k")}, toi_game(), toi_lineups())


# Fetching: nhl toi-reports


def make_api(store: RawStore, handler: Handler) -> NhlApi:
    ticks = iter(range(10_000))
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=nhl_api.BASE_URL,
        headers={"User-Agent": nhl_api.USER_AGENT},
    )
    return NhlApi(
        store, client, min_interval_s=0, now=lambda: FETCHED + timedelta(seconds=next(ticks))
    )


def serve_reports(missing: str = "") -> tuple[list[httpx.Request], Handler]:
    """The trimmed reports of the fixture game by page name; the page named missing is a 404."""
    requests: list[httpx.Request] = []
    pages = {"TH021291.HTM": toi_page("home"), "TV021291.HTM": toi_page("visitor")}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        name = request.url.path.rsplit("/", 1)[-1]
        if name == missing or name not in pages:
            return httpx.Response(404)
        return httpx.Response(200, content=pages[name])

    return requests, handler


def fail(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no request expected, got {request.url}")


def test_reports_are_fetched_once_under_their_own_kinds(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    requests, handler = serve_reports()
    summary = fetch_reports(make_api(store, handler), [(TOI_SEASON, TOI_GAME)])
    assert [str(r.url) for r in requests] == [
        "https://www.nhl.com/scores/htmlreports/20242025/TH021291.HTM",
        "https://www.nhl.com/scores/htmlreports/20242025/TV021291.HTM",
    ]
    # The pages are asked for as a browser would, the NHL API hosts with the client's own agent.
    assert all(r.headers["user-agent"] == BROWSER_USER_AGENT for r in requests)
    for side in ("home", "visitor"):
        key = store.latest(f"nhl/toi-{side}/{TOI_SEASON}/{TOI_GAME}")
        assert key is not None and key.startswith(f"nhl/toi-{side}/20242025/2024021291/2026")
        assert store.get(key) == toi_page(side)
        assert store.meta(key)["url"].endswith(".HTM")
    assert summary.reports == summary.fetched == 2 and summary.stored_before == 0
    assert (summary.players, summary.shifts, summary.problems) == (6, sum(SHIFTS.values()), [])

    # A rerun finds both pages stored and makes no request.
    again = fetch_reports(make_api(store, fail), [(TOI_SEASON, TOI_GAME)])
    assert (again.stored_before, again.fetched, again.shifts) == (2, 0, summary.shifts)


def test_games_outside_the_owners_decision_are_refused_before_any_request(
    tmp_path: Path,
) -> None:
    assert 2024021235 in ALLOWED_GAMES and 2024021291 in ALLOWED_GAMES
    assert 2024021234 not in ALLOWED_GAMES and 2024021292 not in ALLOWED_GAMES
    api = make_api(RawStore(tmp_path), fail)
    games = [(TOI_SEASON, TOI_GAME), (20132014, 2013020971)]
    with pytest.raises(ValueError, match=r"outside the owner's decision.*2013020971"):
        fetch_reports(api, games)
    assert api.requests == 0


def test_a_missing_or_wrong_page_is_reported(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    _, handler = serve_reports(missing="TV021291.HTM")
    summary = fetch_reports(make_api(store, handler), [(TOI_SEASON, TOI_GAME)])
    assert summary.fetched == 1 and len(summary.problems) == 1
    assert "404" in summary.problems[0]
    assert store.latest(f"nhl/toi-visitor/{TOI_SEASON}/{TOI_GAME}") is None

    # A stored page of another game is reported, not counted.
    other = RawStore(tmp_path / "other")
    store_reports(other, game_id=2024021290)
    summary = fetch_reports(make_api(other, fail), [(TOI_SEASON, 2024021290)])
    assert summary.players == 0 and len(summary.problems) == 2
    assert all("is for game 1291" in problem for problem in summary.problems)


def test_chartless_games_are_those_whose_chart_gave_no_shifts() -> None:
    coverage = pl.DataFrame(
        {
            "season": [TOI_SEASON] * 3,
            "game_id": [2024021234, 2024021235, 2024021236],
            "shift_rows": [790, 0, 760],
            "raw_key": [
                toi_key("shiftcharts", 2024021234),
                toi_key("shiftcharts", 2024021235),
                # Already built from its reports by an earlier ingest: a rerun finds it stored.
                toi_key("toi-home", 2024021236),
            ],
        }
    )
    assert chartless_games(coverage) == [(TOI_SEASON, 2024021235), (TOI_SEASON, 2024021236)]


def test_cached_reports_need_both_pages(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    cached = CachedReports(store, print)
    assert cached.find(TOI_SEASON, TOI_GAME) is None
    meta = {"fetched_utc": FETCHED.isoformat()}
    store.put("nhl", f"toi-home/{TOI_SEASON}/{TOI_GAME}/{STAMP}", toi_page("home"), meta)
    assert cached.find(TOI_SEASON, TOI_GAME) is None
    store.put("nhl", f"toi-visitor/{TOI_SEASON}/{TOI_GAME}/{STAMP}", toi_page("visitor"), meta)
    assert cached.find(TOI_SEASON, TOI_GAME) == toi_reports()


# The ingest's fallback, from the raw cache only


def test_an_empty_chart_takes_its_shifts_from_the_cached_reports(tmp_path: Path) -> None:
    store, warnings = RawStore(tmp_path), []
    store_reports(store)
    tables = parse_feeds(toi_games_row(), toi_feeds(), CachedReports(store, warnings.append))
    built = parse_reports(toi_reports(), toi_game(), toi_lineups())
    assert tables["shifts"].equals(built.shifts)
    coverage = tables["shift_coverage"].row(0, named=True)
    # One raw key per coverage row: the home team's report.
    assert coverage["raw_key"] == toi_key("toi-home")
    assert (coverage["shift_rows"], coverage["dropped_rows"], coverage["bad_rows"]) == (76, 0, 0)
    # The same completeness check as a chart's: every dressed player's shifts add up to his
    # boxscore time on ice (each backup goalie dressed without playing).
    assert (coverage["players_dressed"], coverage["players_without_shifts"]) == (8, 0)
    assert coverage["players_toi_off"] == 0 and coverage["complete"]
    assert tables["strength_time"]["seconds"].sum() == 2 * 1200
    assert warnings == []


def test_an_empty_chart_without_both_reports_keeps_no_shifts(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    meta = {"fetched_utc": FETCHED.isoformat()}
    store.put("nhl", f"toi-home/{TOI_SEASON}/{TOI_GAME}/{STAMP}", toi_page("home"), meta)
    tables = parse_feeds(toi_games_row(), toi_feeds(), CachedReports(store, print))
    assert tables["shifts"].is_empty()
    coverage = tables["shift_coverage"].row(0, named=True)
    assert (coverage["raw_key"], coverage["shift_rows"]) == (toi_key("shiftcharts"), 0)
    assert not coverage["complete"]


def test_a_chart_with_shifts_is_untouched(tmp_path: Path) -> None:
    """Reports stored for a game whose chart has shifts are never read: these pages are another
    game's, so reading them would fail."""
    game_id = 2010020003
    store = RawStore(tmp_path)
    store_reports(store, season=20102011, game_id=game_id)
    game = feed_game(game_id)
    kinds = ("play-by-play", "boxscore", "shiftcharts")
    feeds = {kind: (feed(kind, game_id), raw_key(kind, game)) for kind in kinds}
    tables = parse_feeds(games_row(game_id), feeds, CachedReports(store, print))
    without = parsed_feeds(game_id)
    for table in FEED_TABLES:
        assert tables[table].equals(without[table]), table
    assert tables["shift_coverage"]["raw_key"].item().startswith("nhl/shiftcharts/")


def serve_feeds(boxscore: bytes) -> tuple[list[httpx.Request], Handler]:
    """The fixture game's synthetic play-by-play, boxscore and empty shift chart; nothing else."""
    requests: list[httpx.Request] = []
    bodies = {
        f"/v1/gamecenter/{TOI_GAME}/play-by-play": synthetic_play_by_play(),
        f"/v1/gamecenter/{TOI_GAME}/boxscore": boxscore,
        "/stats/rest/en/shiftcharts": EMPTY_CHART,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path not in bodies:
            raise AssertionError(f"the ingest must not request {request.url}")
        return httpx.Response(200, content=bodies[request.url.path])

    return requests, handler


def ingest_game(store: RawStore, boxscore: bytes, lines: list[str]) -> dict[str, pl.DataFrame]:
    requests, handler = serve_feeds(boxscore)
    ingest = Ingest(
        api=make_api(store, handler),
        lake=Lake(store.base_dir.parent / "lake"),
        supabase=None,
        feeds=True,
        players=False,
        echo=lines.append,
    )
    frames: dict[str, list[pl.DataFrame]] = {table: [] for table in FEED_TABLES}
    ingest.game_feeds(toi_games_row(), frames)
    assert {r.url.host for r in requests} == {"api-web.nhle.com", "api.nhle.com"}
    return {table: pl.concat(parts) for table, parts in frames.items()}


def test_the_ingest_reads_reports_from_the_cache_and_never_fetches_one(tmp_path: Path) -> None:
    lines: list[str] = []
    tables = ingest_game(RawStore(tmp_path / "raw"), toi_boxscore(), lines)
    assert tables["shifts"].is_empty() and lines == []

    store = RawStore(tmp_path / "cached" / "raw")
    store_reports(store)
    tables = ingest_game(store, toi_boxscore(), lines)
    assert tables["shifts"].height == sum(SHIFTS.values())
    assert set(tables["shifts"]["raw_key"]) == {toi_key("toi-home"), toi_key("toi-visitor")}
    assert tables["shift_coverage"]["complete"].item() and lines == []


def test_the_ingest_warns_of_each_problem_in_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    box = json.loads(toi_boxscore())
    forwards = box["playerByGameStats"]["homeTeam"]["forwards"]
    box["playerByGameStats"]["homeTeam"]["forwards"] = [
        p for p in forwards if p["sweaterNumber"] != LAUKO
    ]
    store, lines = RawStore(tmp_path / "raw"), []
    store_reports(store)
    tables = ingest_game(store, json.dumps(box).encode(), lines)
    assert lines == [
        f"warning: {TOI_GAME}: home report: 'LAUKO, JAKUB' has a number not in BOS's boxscore; "
        "17 shifts counted as bad rows"
    ]
    coverage = tables["shift_coverage"].row(0, named=True)
    assert (coverage["bad_rows"], coverage["complete"]) == (17, False)
