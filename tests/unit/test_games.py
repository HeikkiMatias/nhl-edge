from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandera.errors
import polars as pl
import pytest

from nhl_edge.ingest.games import (
    EXPECTED_GAMES,
    RESULT_LAG,
    listed_games,
    parse_games,
    probe_date,
    season_bounds,
    settled_on,
)
from nhl_edge.lake.schemas import Games

FIXTURES = Path(__file__).parent / "fixtures" / "nhl_api"
OPENING_WEEK = (FIXTURES / "schedule_2010-10-07.json").read_bytes()
PANDEMIC_WEEK = (FIXTURES / "schedule_2021-01-13.json").read_bytes()
SEASON_END_WEEK = (FIXTURES / "schedule_2026-04-13.json").read_bytes()
UPCOMING_WEEK = (FIXTURES / "schedule_2026-09-28.json").read_bytes()


def week_days(start: date) -> set[date]:
    return {start + timedelta(days=i) for i in range(7)}


def games_of(body: bytes, start: date) -> pl.DataFrame:
    return parse_games(listed_games(body, week_days(start)), "nhl/schedule/test")


OPENING = games_of(OPENING_WEEK, date(2010, 10, 7))


def row(game_id: int) -> dict[str, Any]:
    return OPENING.filter(pl.col("game_id") == game_id).row(0, named=True)


def test_regulation_overtime_and_shootout() -> None:
    assert dict(OPENING.select("game_id", "decided_in").iter_rows()) == {
        2010020003: "REG",
        2010020004: "OT",
        2010020008: "SO",
    }
    # Full-game scores: the shootout winner is credited with one goal.
    so = row(2010020008)
    assert abs(int(so["home_score"]) - int(so["away_score"])) == 1


def test_neutral_site_and_venue() -> None:
    helsinki = row(2010020003)
    assert (helsinki["home"], helsinki["away"]) == ("MIN", "CAR")
    assert helsinki["venue"] == "Hartwall Areena"
    assert helsinki["neutral_site"] is True
    assert row(2010020004)["neutral_site"] is False


def test_game_date_is_the_nhl_date_not_the_utc_date() -> None:
    late = row(2010020004)
    assert late["start_utc"] == datetime(2010, 10, 8, 2, 0, tzinfo=UTC)
    assert late["game_date"] == date(2010, 10, 7)


def test_result_is_observed_six_hours_after_the_start() -> None:
    assert (OPENING["observed_utc"] - OPENING["start_utc"] == RESULT_LAG).all()
    assert OPENING["raw_key"].unique().to_list() == ["nhl/schedule/test"]


def test_only_the_requested_days_are_kept() -> None:
    first_day = parse_games(listed_games(OPENING_WEEK, {date(2010, 10, 7)}), "k")
    assert first_day["game_id"].to_list() == [2010020003, 2010020004]


def test_limited_attendance_only_in_2020_21() -> None:
    pandemic = games_of(PANDEMIC_WEEK, date(2021, 1, 13))
    assert pandemic.height == 2
    assert pandemic["limited_attendance"].all()
    assert not OPENING["limited_attendance"].any()


def test_playoff_games_are_not_listed() -> None:
    days = week_days(date(2026, 4, 13))
    assert [g["id"] for _, g in listed_games(SEASON_END_WEEK, days)] == [2025021309]


def test_games_not_final_are_listed_but_not_parsed() -> None:
    days = week_days(date(2026, 9, 28))
    listed = listed_games(UPCOMING_WEEK, days)
    assert len(listed) == 8
    assert {g["gameState"] for _, g in listed} == {"FUT"}
    assert parse_games(listed, "k").is_empty()
    assert settled_on(days)(UPCOMING_WEEK, {}) is False
    assert settled_on(week_days(date(2010, 10, 7)))(OPENING_WEEK, {}) is True


def test_season_bounds_and_probe() -> None:
    assert season_bounds(OPENING_WEEK) == (date(2010, 10, 7), date(2011, 4, 10))
    assert season_bounds(PANDEMIC_WEEK) == (date(2021, 1, 13), date(2021, 5, 19))
    assert probe_date(20122013) == date(2013, 2, 15)


def test_expected_game_counts() -> None:
    assert sum(EXPECTED_GAMES.values()) == 19_152
    assert min(EXPECTED_GAMES) == 20102011
    assert max(EXPECTED_GAMES) == 20252026


@pytest.mark.parametrize(
    ("change", "check"),
    [
        ({"home_score": 3, "away_score": 3}, "no_ties"),
        ({"decided_in": "OT", "home_score": 5, "away_score": 3}, "extra_time_wins_by_one"),
        ({"game_id": 2011020003}, "regular_season_id_of_its_season"),
        ({"game_id": 2010030003}, "regular_season_id_of_its_season"),
        ({"home": "CAR"}, "home_is_not_away"),
    ],
)
def test_schema_rejects_impossible_games(change: dict[str, object], check: str) -> None:
    bad = OPENING.head(1).with_columns(
        pl.lit(value, dtype=OPENING.schema[column]).alias(column)
        for column, value in change.items()
    )
    with pytest.raises(pandera.errors.SchemaError, match=check):
        Games.validate(bad)
