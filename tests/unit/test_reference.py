from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandera.errors
import polars as pl
import pytest

from nhl_edge.ingest.games import EXPECTED_GAMES, listed_games, parse_games, schedule_of
from nhl_edge.lake.schemas import Arenas
from nhl_edge.reference import Reference, capacity_share, check_files, check_games, lineage

FIXTURES = Path(__file__).parent / "fixtures" / "nhl_api"
REF = Reference.load()


def week(name: str, start: date) -> pl.DataFrame:
    days = {start + timedelta(days=i) for i in range(7)}
    return parse_games(listed_games((FIXTURES / name).read_bytes(), days), "nhl/schedule/test")


# MIN and CAR in Helsinki twice (neutral site) and CHI at COL at the Pepsi Center.
OPENING = week("schedule_2010-10-07.json", date(2010, 10, 7))
WEEKS = {
    "opening 2010": OPENING,
    "empty arenas 2021": week("schedule_2021-01-13.json", date(2021, 1, 13)),
    "Lake Tahoe 2021": week("schedule_2021-02-20.json", date(2021, 2, 20)),
    "season end 2026": week("schedule_2026-04-13.json", date(2026, 4, 13)),
}
HELSINKI, CHI_AT_COL, HELSINKI_REMATCH = 2010020003, 2010020004, 2010020008


def season(team: str, frame: pl.DataFrame = REF.teams) -> tuple[int, int | None]:
    row = frame.filter(pl.col("team") == team).row(0, named=True)
    return row["first_season"], row["last_season"]


def test_the_files_agree_with_each_other() -> None:
    assert check_files(REF) == []


@pytest.mark.parametrize("games", WEEKS.values(), ids=list(WEEKS))
def test_real_weeks_map_to_one_arena_code_and_coach(games: pl.DataFrame) -> None:
    assert games.height > 0
    assert check_games(games, REF) == []


def test_the_team_code_history() -> None:
    assert season("ATL") == (20102011, 20102011)
    assert season("WPG") == (20112012, None)
    assert season("PHX") == (20102011, 20132014)
    assert season("ARI") == (20142015, 20232024)
    assert season("UTA") == (20242025, None)
    assert season("VGK") == (20172018, None)
    assert season("SEA") == (20212022, None)
    predecessors = REF.teams.filter(pl.col("predecessor").is_not_null())
    assert dict(predecessors.select("team", "predecessor").iter_rows()) == {
        "ARI": "PHX",
        "UTA": "ARI",
        "WPG": "ATL",
    }


def test_30_31_then_32_teams() -> None:
    for first, last, teams in ((2010, 2016, 30), (2017, 2020, 31), (2021, 2026, 32)):
        for year in range(first, last + 1):
            active = REF.teams.filter(
                (pl.col("first_season") <= year * 10_001 + 1)
                & (pl.col("last_season").fill_null(99_999_999) >= year * 10_001 + 1)
            )
            assert active.height == teams, year


def test_the_codes_of_one_team_share_a_lineage() -> None:
    line = lineage(REF.teams)
    assert line["ATL"] == line["WPG"] == "ATL"
    assert line["PHX"] == line["ARI"] == line["UTA"] == "PHX"
    assert line["BOS"] == "BOS"
    with pytest.raises(ValueError, match="cycle"):
        lineage(REF.teams.with_columns(predecessor=pl.col("predecessor").fill_null("ATL")))


def test_every_time_zone_is_near_its_longitude() -> None:
    # Standard time (January) is within two hours of solar time everywhere the NHL has played,
    # which catches a wrong zone or a sign error in the longitude.
    for arena, tz, longitude in REF.arenas.select("arena_id", "tz", "longitude").iter_rows():
        local = pl.Series([datetime(2026, 1, 15, 12, tzinfo=UTC)]).dt.convert_time_zone(tz)
        offset = local.dt.base_utc_offset()[0]
        assert abs(offset / timedelta(hours=1) - longitude / 15) < 2, arena


def test_a_zone_must_be_a_canonical_iana_zone() -> None:
    for tz in ("US/Eastern", "America/Nowhere"):
        with pytest.raises(pandera.errors.SchemaError):
            Arenas.validate(REF.arenas.with_columns(tz=pl.lit(tz)))


def test_a_coach_stint_runs_through_a_change_of_code() -> None:
    # André Tourigny's stint started under ARI and covers UTA's games.
    tourigny = REF.coaches.filter(pl.col("coach") == "André Tourigny").row(0, named=True)
    assert (tourigny["team"], tourigny["last_game"]) == ("ARI", None)
    utah = pl.DataFrame(
        {
            "game_id": [2024020001],
            "season": [20242025],
            "game_date": [date(2024, 10, 8)],
            "home": ["UTA"],
            "away": ["CHI"],
            "venue": ["Delta Center"],
            "neutral_site": [False],
        }
    )
    assert check_games(utah, REF) == []


def game(game_id: int, **changes: Any) -> pl.DataFrame:
    """OPENING with one game's columns changed."""
    row = pl.col("game_id") == game_id
    return OPENING.with_columns(
        **{
            column: pl.when(row).then(pl.lit(value)).otherwise(column)
            for column, value in changes.items()
        }
    )


def without(frame: pl.DataFrame, **match: Any) -> pl.DataFrame:
    condition = pl.lit(True)
    for column, value in match.items():
        condition = condition & (pl.col(column) == value)
    return frame.filter(~condition)


COL_SACCO = {"team": "COL", "first_game": date(2009, 10, 1)}
CASES: dict[str, tuple[pl.DataFrame, Reference, str]] = {
    "unknown venue": (game(CHI_AT_COL, venue="Nowhere Arena"), REF, "venue 'Nowhere Arena'"),
    "unknown code": (game(CHI_AT_COL, away="XXX"), REF, "XXX plays in 20102011"),
    "code outside its seasons": (
        game(CHI_AT_COL, away="VGK"),
        REF,
        "VGK plays in 20102011 (1 games, e.g. 2010020004), outside its seasons",
    ),
    "home game away from home": (
        game(HELSINKI, neutral_site=False),
        REF,
        "MIN hosts at Hartwall Areena in 20102011",
    ),
    "neutral site at home": (
        game(CHI_AT_COL, neutral_site=True),
        REF,
        "neutral-site games at COL's home arena Pepsi Center",
    ),
    "no primary home arena": (
        OPENING,
        replace(REF, home_arenas=without(REF.home_arenas, team="COL")),
        "COL has 0 primary home arenas in 20102011",
    ),
    "two primary home arenas": (
        OPENING,
        replace(
            REF,
            home_arenas=pl.concat(
                [
                    REF.home_arenas,
                    REF.home_arenas.filter(pl.col("team") == "COL").with_columns(
                        arena_id=pl.lit("coors_field")
                    ),
                ]
            ),
        ),
        "COL has 2 primary home arenas in 20102011",
    ),
    "no coach": (
        OPENING,
        replace(REF, coaches=without(REF.coaches, **COL_SACCO)),
        "COL: 0 coach stints cover 1 of its games (2010-10-07 to 2010-10-07)",
    ),
    "overlapping coaches": (
        OPENING,
        replace(
            REF,
            coaches=pl.concat(
                [
                    REF.coaches,
                    REF.coaches.filter(
                        (pl.col("team") == "COL") & (pl.col("first_game") == date(2009, 10, 1))
                    ).with_columns(first_game=pl.lit(date(2010, 1, 1))),
                ]
            ),
        ),
        "COL: 2 coach stints cover 1 of its games",
    ),
    "stint starts off a game day": (
        # CAR plays in Helsinki on 10-07 and, moved a day, on 10-09: a stint starting between
        # the two starts on a day CAR does not play.
        game(HELSINKI_REMATCH, game_date=date(2010, 10, 9)),
        replace(
            REF,
            coaches=REF.coaches.with_columns(
                first_game=pl.when((pl.col("team") == "CAR") & (pl.col("coach") == "Paul Maurice"))
                .then(pl.lit(date(2010, 10, 8)))
                .otherwise("first_game")
            ),
        ),
        "coaches.csv: CAR first_game 2010-10-08 is not a game of that team",
    ),
    "venue of a missing arena": (
        OPENING,
        replace(
            REF,
            venues=REF.venues.with_columns(
                arena_id=pl.when(pl.col("venue") == "Pepsi Center")
                .then(pl.lit("pepsi"))
                .otherwise("arena_id")
            ),
        ),
        "venues.csv: arena pepsi is not in arenas.csv",
    ),
    "a gap between codes": (
        OPENING,
        replace(
            REF,
            teams=REF.teams.with_columns(
                first_season=pl.when(pl.col("team") == "WPG")
                .then(20122013)
                .otherwise("first_season")
            ),
        ),
        "teams.csv: WPG starts in 20122013, not the season after ATL ends",
    ),
}


@pytest.mark.parametrize("case", list(CASES))
def test_each_kind_of_problem_is_found(case: str) -> None:
    games, ref, expected = CASES[case]
    problems = check_games(games, ref)
    assert any(expected in problem for problem in problems), problems


def test_a_finished_season_must_have_every_code_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pretend the three opening games were the whole season: every other team is missing.
    monkeypatch.setitem(EXPECTED_GAMES, 20102011, OPENING.height)
    problems = check_games(OPENING, REF)
    assert "ATL is in use in 20102011 by teams.csv but plays no game" in problems
    assert not any(problem.startswith("CHI is in use") for problem in problems)


PRIMARY_NOT_OPENER = replace(
    REF,
    home_arenas=pl.concat(
        [
            REF.home_arenas.with_columns(primary=pl.col("primary") & (pl.col("team") != "COL")),
            REF.home_arenas.filter(pl.col("team") == "COL").with_columns(
                arena_id=pl.lit("coors_field"), primary=pl.lit(True)
            ),
        ]
    ),
)


def test_the_primary_home_arena_hosts_the_season_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    # COL's primary arena is moved to Coors Field, but its first home game is at the Pepsi Center.
    # Only a finished season is sure to hold the opener, so part of one isn't checked.
    expected = "COL's first home game in 20102011 (2010020004) is at Pepsi Center, not its primary"
    assert not any(expected in problem for problem in check_games(OPENING, PRIMARY_NOT_OPENER))
    monkeypatch.setitem(EXPECTED_GAMES, 20102011, OPENING.height)
    assert any(expected in problem for problem in check_games(OPENING, PRIMARY_NOT_OPENER))


def test_a_later_home_game_at_a_second_arena_is_fine() -> None:
    # NYI's 2018-19 home games moved to the Coliseum from December; Barclays was primary.
    coliseum = pl.DataFrame(
        {
            "game_id": [2018020350],
            "season": [20182019],
            "game_date": [date(2018, 12, 1)],
            "home": ["NYI"],
            "away": ["DAL"],
            "venue": ["NYCB Live/Nassau Coliseum"],
            "neutral_site": [False],
        }
    )
    assert check_games(coliseum, REF) == []


def played(*games: tuple[str, str, date]) -> pl.DataFrame:
    """Schedule rows as (home, venue, date), one id each, public the day before."""
    days = [day for _, _, day in games]
    return pl.DataFrame(
        {
            "game_id": list(range(1, len(games) + 1)),
            "home": [home for home, _, _ in games],
            "venue": [venue for _, venue, _ in games],
            "game_date": days,
            "observed_utc": [
                datetime.combine(day, time(0), tzinfo=UTC) - timedelta(days=1) for day in days
            ],
        }
    )


def game_day(day: date) -> datetime:
    """The morning odds slot on a game's date, 11:05 UTC."""
    return datetime(day.year, day.month, day.day, 11, 5, tzinfo=UTC)


def shares(*games: tuple[str, str, date]) -> list[float]:
    """Each game's share as predicted on the morning of its date."""
    frame = played(*games)
    return [
        capacity_share(frame, game_day(day), [game_id], REF)
        .filter(pl.col("game_id") == game_id)["capacity_share"]
        .item()
        for game_id, day in frame.select("game_id", "game_date").iter_rows()
    ]


def test_capacity_share_by_arena_and_date() -> None:
    assert shares(
        # 2020-21: Dallas at 25% all season, Detroit empty until 750 fans from March 9.
        ("DAL", "American Airlines Center", date(2021, 1, 22)),
        ("DET", "Little Caesars Arena", date(2021, 3, 7)),
        ("DET", "Little Caesars Arena", date(2021, 3, 9)),
        # Canada was empty all season, Lake Tahoe too.
        ("TOR", "Scotiabank Arena", date(2021, 4, 1)),
        ("COL", "Edgewood Tahoe Resort", date(2021, 2, 20)),
        # 2021-22: full in the US, Ontario at 500 from January 31 and 50% from February 17.
        ("BOS", "TD Garden", date(2022, 1, 15)),
        ("TOR", "Scotiabank Arena", date(2022, 1, 31)),
        ("OTT", "Canadian Tire Centre", date(2022, 2, 19)),
        ("TOR", "Scotiabank Arena", date(2022, 3, 2)),
        # An unknown venue has no limit to find.
        ("TOR", "Nowhere Arena", date(2021, 4, 1)),
    ) == [0.25, 0.0, round(750 / 19_515, 3), 0.0, 0.0, 1.0, round(500 / 18_819, 3), 0.5, 1.0, 1.0]


def test_every_game_of_the_empty_arenas_week_is_limited() -> None:
    # The fixture week's home teams, Philadelphia and Toronto, had no spectators yet.
    schedule = schedule_of(WEEKS["empty arenas 2021"])
    by_home = [
        (home, capacity_share(schedule, game_day(day), [game_id], REF)["capacity_share"].item())
        for game_id, home, day in schedule.select("game_id", "home", "game_date").iter_rows()
    ]
    assert sorted({home for home, _ in by_home}) == ["PHI", "TOR"]
    assert [share for _, share in by_home] == [0.0] * schedule.height


ATTENDANCE_CASES: dict[str, tuple[pl.DataFrame, Reference, str]] = {
    "limit at an unknown arena": (
        OPENING,
        replace(
            REF,
            attendance_limits=REF.attendance_limits.with_columns(
                arena_id=pl.when(pl.col("arena_id") == "ball_arena")
                .then(pl.lit("pepsi"))
                .otherwise("arena_id")
            ),
        ),
        "attendance_limits.csv: arena pepsi is not in arenas.csv",
    ),
    "overlapping limits": (
        OPENING,
        replace(
            REF,
            attendance_limits=REF.attendance_limits.with_columns(
                last_date=pl.when(
                    (pl.col("arena_id") == "ball_arena")
                    & (pl.col("first_date") == date(2021, 1, 13))
                )
                .then(pl.lit(date(2021, 4, 2)))
                .otherwise("last_date")
            ),
        ),
        "the ball_arena limit from 2021-01-13 overlaps the one from 2021-04-02",
    ),
    "limited season without a limit": (
        WEEKS["empty arenas 2021"],
        replace(
            REF,
            attendance_limits=REF.attendance_limits.filter(
                pl.col("arena_id") != "xfinity_mobile_arena"
            ),
        ),
        "20202021 is limited_attendance, but attendance_limits.csv has no limit for",
    ),
    "unannounced limit mid-season": (
        WEEKS["empty arenas 2021"],
        replace(
            REF,
            attendance_limits=REF.attendance_limits.with_columns(
                announced=pl.when(pl.col("arena_id") == "united_center")
                .then(None)
                .otherwise("announced")
            ),
        ),
        "the united_center limit from 2021-05-09 has no announcement date",
    ),
}


VANCOUVER_AFTER_THE_LIFT = pl.DataFrame(
    {
        "game_id": [2021020870],
        "season": pl.Series([20212022], dtype=pl.Int32),
        "game_date": [date(2022, 2, 19)],
        "home": ["VAN"],
        "away": ["TOR"],
        "venue": ["Rogers Arena"],
        "neutral_site": [False],
    }
)
ATTENDANCE_CASES["lift without its announcement"] = (
    VANCOUVER_AFTER_THE_LIFT,
    replace(
        REF,
        attendance_limits=REF.attendance_limits.with_columns(
            ended_announced=pl.when(pl.col("arena_id") == "rogers_arena")
            .then(None)
            .otherwise("ended_announced")
        ),
    ),
    "the rogers_arena limit from 2021-12-20 ends on 2022-02-16, before a game there that season",
)


def test_the_real_lift_is_recorded() -> None:
    assert check_games(VANCOUVER_AFTER_THE_LIFT, REF) == []


@pytest.mark.parametrize("case", list(ATTENDANCE_CASES))
def test_each_kind_of_attendance_problem_is_found(case: str) -> None:
    games, ref, expected = ATTENDANCE_CASES[case]
    problems = check_games(games, ref)
    assert any(expected in problem for problem in problems), problems
