from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import polars as pl
import pytest

from nhl_edge.ingest.nhl_api import NotCachedError
from nhl_edge.ingest.sbr import (
    BROWSER_USER_AGENT,
    SbrArchive,
    american_to_decimal,
    import_seasons,
    match_season,
    parse_season,
    report_lines,
    team_code,
)
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import Games, SbrOdds, Schedule, dtypes
from nhl_edge.lake.tables import Lake

FIXTURES = Path(__file__).parent / "fixtures" / "sbr"
OLD = (FIXTURES / "nhl-odds-2010-11.html").read_bytes()  # moneyline and totals only
NEW = (FIXTURES / "nhl-odds-2021-22.html").read_bytes()  # adds the closing puck line
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def schedule(rows: list[tuple[int, int, date, datetime, str, str]]) -> pl.DataFrame:
    frame = pl.DataFrame(
        [
            {
                "game_id": game_id,
                "season": season,
                "game_date": game_date,
                "start_utc": start,
                "home": home,
                "away": away,
                "venue": "x",
                "neutral_site": False,
                "limited_attendance": False,
                "observed_utc": start.replace(hour=0),
                "raw_key": "nhl/schedule/x",
            }
            for game_id, season, game_date, start, home, away in rows
        ],
        schema=dtypes(Schedule),
    )
    return frame


def results(rows: list[tuple[int, int, int]], sched: pl.DataFrame) -> pl.DataFrame:
    scores = pl.DataFrame(
        rows,
        schema={"game_id": pl.Int64, "home_score": pl.Int16, "away_score": pl.Int16},
        orient="row",
    )
    return (
        sched.join(scores, on="game_id")
        .with_columns(decided_in=pl.lit("REG"))
        .select(list(dtypes(Games)))
    )


OLD_SCHEDULE = schedule(
    [
        (
            2010020001,
            20102011,
            date(2010, 10, 7),
            datetime(2010, 10, 7, 16, tzinfo=UTC),
            "MIN",
            "CAR",
        ),
        (
            2010020500,
            20102011,
            date(2010, 12, 31),
            datetime(2011, 1, 1, 0, tzinfo=UTC),
            "STL",
            "NYR",
        ),
        (
            2010020600,
            20102011,
            date(2011, 1, 2),
            datetime(2011, 1, 2, 23, tzinfo=UTC),
            "TBL",
            "WPG",
        ),
    ]
)
NEW_SCHEDULE = schedule(
    [
        (
            2021020001,
            20212022,
            date(2021, 10, 12),
            datetime(2021, 10, 12, 23, tzinfo=UTC),
            "TBL",
            "PIT",
        ),
        # Neutral: SBR lists ARI first, the NHL has SJS as the home team.
        (
            2021020010,
            20212022,
            date(2021, 10, 13),
            datetime(2021, 10, 13, 18, tzinfo=UTC),
            "SJS",
            "ARI",
        ),
        (
            2021020020,
            20212022,
            date(2021, 10, 14),
            datetime(2021, 10, 15, 2, tzinfo=UTC),
            "SEA",
            "VGK",
        ),
        (
            2021020030,
            20212022,
            date(2021, 10, 15),
            datetime(2021, 10, 15, 23, tzinfo=UTC),
            "BOS",
            "DAL",
        ),
    ]
)


def quotes(
    frame: pl.DataFrame, game_id: int
) -> dict[tuple[str, str, str], tuple[float | None, int]]:
    rows = frame.filter(pl.col("game_id") == game_id).select(
        "market", "quote", "side", "line", "price_american"
    )
    return {(m, q, s): (line, price) for m, q, s, line, price in rows.iter_rows()}


def test_american_odds_convert_to_decimal() -> None:
    assert american_to_decimal(150) == 2.5
    assert american_to_decimal(-200) == 1.5
    assert american_to_decimal(100) == american_to_decimal(-100) == 2.0
    with pytest.raises(ValueError):
        american_to_decimal(50)


def test_team_names_map_to_tricodes_whatever_the_spacing() -> None:
    assert team_code("St. Louis") == team_code("St.Louis") == "STL"
    assert team_code("NY Islanders") == team_code("NYIslanders") == "NYI"
    assert team_code("Seattle Kraken") == team_code("Seattle") == "SEA"
    assert team_code("Arizonas") == "ARI"
    assert team_code("Phoenix") == "PHX"
    assert team_code("WinnipegJets") == "WPG"
    with pytest.raises(ValueError, match="Hartford"):
        team_code("Hartford")


def test_old_layout_has_moneylines_and_totals() -> None:
    games = parse_season(OLD, 20102011)
    assert [g.game_date for g in games] == [
        date(2010, 10, 7),
        date(2010, 12, 31),
        date(2011, 1, 2),
        date(2011, 4, 15),
    ]
    first = games[0]
    assert first.teams == ("CAR", "MIN")
    assert first.finals == (4, 3)
    assert {(m, q) for m, q, *_ in first.quotes} == {
        ("h2h", "open"),
        ("h2h", "close"),
        ("totals", "open"),
        ("totals", "close"),
    }
    # The first row's total price is the over.
    totals = {
        (q, side): (line, price) for m, q, _, side, line, price in first.quotes if m == "totals"
    }
    assert totals[("close", "over")] == (5.5, 110)
    assert totals[("close", "under")] == (5.5, -130)


def test_a_price_shown_as_nl_or_blank_is_left_out_and_counted() -> None:
    old, new = parse_season(OLD, 20102011), parse_season(NEW, 20212022)
    assert old[1].missing == 1
    # The Rangers' opener is NL, so the Blues' opener goes too: de-vigging needs both.
    assert len(old[1].quotes) == 6
    assert ("h2h", "open") not in {(m, q) for m, q, *_ in old[1].quotes}
    assert new[2].missing == 1


def test_new_layout_adds_the_closing_puck_line() -> None:
    game = parse_season(NEW, 20212022)[0]
    spreads = [(team, line, price) for m, q, team, _, line, price in game.quotes if m == "spreads"]
    assert spreads == [(0, 1.5, -120), (1, -1.5, 100)]
    assert all(q == "close" for m, q, *_ in game.quotes if m == "spreads")


def test_an_unknown_layout_is_refused() -> None:
    with pytest.raises(ValueError, match="no SBR odds table"):
        parse_season(b"<html><table><tr><td>Rot</td></tr></table></html>", 20102011)
    broken = NEW.replace(b"<td>V</td>", b"<td>H</td>", 1)
    with pytest.raises(ValueError, match="not one game"):
        parse_season(broken, 20212022)


def test_the_schedule_decides_home_and_away() -> None:
    frame, report = match_season(
        parse_season(NEW, 20212022), NEW_SCHEDULE, results_empty(NEW_SCHEDULE), "sbr/k"
    )
    SbrOdds.validate(frame)
    neutral = quotes(frame, 2021020010)
    # SBR listed ARI (+165 close) first; the NHL has it away.
    assert neutral[("h2h", "close", "away")] == (None, 165)
    assert neutral[("h2h", "close", "home")] == (None, -185)
    # SBR listed SEA as H first, then VGK as V.
    reversed_rows = quotes(frame, 2021020020)
    assert reversed_rows[("h2h", "close", "home")] == (None, -120)
    assert reversed_rows[("spreads", "close", "home")] == (-1.5, 210)
    # The over and under stay with the page's first and second row.
    assert reversed_rows[("totals", "close", "over")] == (6.0, -110)
    assert report.matched == 3
    assert report.nhl_without_sbr == 1
    assert report.join_rate == 0.75


def results_empty(frame: pl.DataFrame) -> pl.DataFrame:
    return pl.DataFrame(schema=dtypes(Games))


def test_games_after_the_regular_season_and_unmatched_games_are_reported() -> None:
    games = parse_season(OLD, 20102011)
    # Drop the 2010-12-31 game from the schedule: it is in the season, so it is unmatched.
    sched = OLD_SCHEDULE.filter(pl.col("game_id") != 2010020500)
    frame, report = match_season(games, sched, results_empty(sched), "sbr/k")
    assert report.sbr_games == 4
    assert report.matched == 2
    assert report.after_regular_season == 1  # the 2011-04-15 playoff game
    assert report.unmatched == [(date(2010, 12, 31), "NYR", "STL")]
    assert set(frame["game_id"]) == {2010020001, 2010020600}


def test_a_final_score_that_disagrees_with_the_nhl_is_reported() -> None:
    games = parse_season(OLD, 20102011)
    scores = results([(2010020001, 3, 4), (2010020500, 3, 1), (2010020600, 4, 2)], OLD_SCHEDULE)
    _, report = match_season(games, OLD_SCHEDULE, scores, "sbr/k")
    assert report.score_mismatches == [(2010020500, "SBR 3-2, NHL 3-1")]


def test_each_price_has_its_decimal_and_times() -> None:
    frame, _ = match_season(
        parse_season(OLD, 20102011), OLD_SCHEDULE, results_empty(OLD_SCHEDULE), "k"
    )
    row = frame.filter(
        (pl.col("game_id") == 2010020600) & (pl.col("market") == "h2h") & (pl.col("side") == "away")
    )
    by_quote = {
        q: (d, observed, assumed)
        for q, d, observed, assumed in row.select(
            "quote", "price_decimal", "observed_utc", "assumed_available_utc"
        ).iter_rows()
    }
    start = datetime(2011, 1, 2, 23, tzinfo=UTC)
    assert by_quote["open"] == (2.5, start, datetime(2011, 1, 2, 15, tzinfo=UTC))  # 10:00 EST
    assert by_quote["close"] == (2.4, start, start)


def page_client(pages: dict[str, bytes], seen: list[httpx.Request]) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "Mozilla" not in request.headers.get("user-agent", ""):
            return httpx.Response(404)
        body = pages.get(request.url.path)
        return httpx.Response(200, content=body) if body else httpx.Response(404)

    return httpx.Client(
        base_url="https://www.sportsbookreviewsonline.com",
        headers={"User-Agent": BROWSER_USER_AGENT},
        transport=httpx.MockTransport(handle),
    )


def test_the_archive_sends_a_browser_user_agent_and_stores_the_page(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    store = RawStore(tmp_path / "raw")
    client = page_client({"/scoresoddsarchives/nhl-odds-2010-11/": OLD}, seen)
    archive = SbrArchive(store, client, now=lambda: NOW, sleep=lambda s: None)
    body, raw_key = archive.page(20102011)
    assert body == OLD
    assert raw_key == "sbr/20102011/20260929T120000Z"
    assert store.get(raw_key) == OLD
    assert seen[0].headers["user-agent"] == BROWSER_USER_AGENT
    # A stored page is reused, without a request.
    assert archive.page(20102011) == (OLD, raw_key)
    assert len(seen) == 1


def test_the_default_client_sends_a_browser_user_agent(tmp_path: Path) -> None:
    archive = SbrArchive(RawStore(tmp_path))
    assert archive.client.headers["user-agent"] == BROWSER_USER_AGENT


def test_replay_never_fetches(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    archive = SbrArchive(RawStore(tmp_path), page_client({}, seen), offline=True)
    with pytest.raises(NotCachedError):
        archive.page(20102011)
    assert seen == []


# The fixture schedules stand in for complete seasons.
FIXTURE_GAMES = {20102011: 3, 20212022: 4}


def test_import_writes_the_lake_table(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    store.put("sbr", "20102011/20260929T120000Z", OLD, {"fetched_utc": NOW.isoformat()})
    store.put("sbr", "20212022/20260929T120000Z", NEW, {"fetched_utc": NOW.isoformat()})
    sched = pl.concat([OLD_SCHEDULE, NEW_SCHEDULE])
    frames, reports = import_seasons(
        SbrArchive(store, offline=True),
        [20102011, 20212022],
        sched,
        results_empty(sched),
        FIXTURE_GAMES,
    )
    lake = Lake(tmp_path / "lake")
    for frame in frames.values():
        lake.write("sbr_odds", frame)
    table = lake.read("sbr_odds")
    assert set(table["season"]) == {20102011, 20212022}
    assert table["raw_key"].str.starts_with("sbr/").all()
    lines = report_lines(reports)
    assert lines[2].startswith("| 20102011 | 4 | 3 | 1 | 0 | 3 | 0 | 100.0%")


def test_import_refuses_a_season_sbr_does_not_have(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="20232024"):
        import_seasons(
            SbrArchive(RawStore(tmp_path), offline=True), [20232024], OLD_SCHEDULE, OLD_SCHEDULE
        )


def test_import_refuses_a_season_whose_schedule_is_incomplete(tmp_path: Path) -> None:
    # Each season replaces its whole partition, so importing part of a season would delete the
    # rest of its prices. The check comes before any page is read.
    seen: list[httpx.Request] = []
    archive = SbrArchive(RawStore(tmp_path), page_client({}, seen))
    partial = OLD_SCHEDULE.head(2)
    with pytest.raises(ValueError, match="2 of 20102011's 3 games"):
        import_seasons(archive, [20102011], partial, results_empty(partial), FIXTURE_GAMES)
    assert seen == []
