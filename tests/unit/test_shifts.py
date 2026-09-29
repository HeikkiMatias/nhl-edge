import json
from typing import Any

import polars as pl
import pytest
from feed_fixtures import TRIMMED_GAMES, feed, feed_game, raw_key, trimmed

from nhl_edge.ingest.shifts import SHIFT, ShiftDrops, parse_shifts, shift_rows


def chart_rows(body: bytes) -> list[dict[str, Any]]:
    return json.loads(body)["data"]


def parse_trimmed(game_id: int) -> tuple[pl.DataFrame, ShiftDrops]:
    return parse_shifts(trimmed("shiftcharts", game_id), TRIMMED_GAMES[game_id], "k")


def test_full_chart() -> None:
    game = feed_game(2010020003)
    body = feed("shiftcharts", 2010020003)
    shifts, drops = parse_shifts(body, game, raw_key("shiftcharts", game))
    shift_count = sum(row["typeCode"] == SHIFT for row in chart_rows(body))
    assert (shifts.height, drops) == (shift_count, ShiftDrops())
    assert set(shifts["team"]) == {"MIN", "CAR"}
    # Andrew Brunette's first shift: 00:00 to 00:52 of the first period.
    first = shifts.filter((pl.col("player_id") == 8459596) & (pl.col("shift_number") == 1))
    assert first.select("period", "start_s", "end_s").row(0) == (1, 0, 52)
    third = shifts.filter(pl.col("period") == 3)
    assert third["start_s"].is_between(2400, 3600).all()


def test_zero_length_placeholders_are_dropped() -> None:
    rows = chart_rows(trimmed("shiftcharts", 2019020003))
    placeholders = [r for r in rows if not r["endTime"]]
    assert placeholders and all(r["duration"] == "00:00" for r in placeholders)
    shifts, drops = parse_trimmed(2019020003)
    assert drops == ShiftDrops(dropped=len(placeholders))
    assert shifts.height == len(rows) - len(placeholders)


def test_shifts_of_teams_not_in_the_game_are_foreign() -> None:
    # The WSH at NYI chart lists every shift of the game twice, under two blocks of row ids, and
    # STL and MIN shifts besides.
    rows = chart_rows(trimmed("shiftcharts", 2021020513))
    shifts, drops = parse_trimmed(2021020513)
    foreign = [r for r in rows if r["teamAbbrev"] in ("STL", "MIN")]
    in_game = len(rows) - len(foreign)
    assert foreign
    assert drops == ShiftDrops(dropped=in_game // 2, foreign=len(foreign))
    assert shifts.height == in_game // 2
    assert set(shifts["team"]) == {"NYI", "WSH"}


def test_shootout_rows_are_dropped_and_overtime_kept() -> None:
    rows = chart_rows(trimmed("shiftcharts", 2021020972))
    shootout = [r for r in rows if r["typeCode"] == SHIFT and r["period"] == 5]
    overtime = [r for r in rows if r["typeCode"] == SHIFT and r["period"] == 4]
    assert shootout and overtime
    assert any(r["typeCode"] != SHIFT for r in rows)  # goal markers are skipped silently
    shifts, drops = parse_trimmed(2021020972)
    assert drops == ShiftDrops(dropped=len(shootout))
    assert shifts.height == len(overtime)
    assert shifts["start_s"].min() == 3600
    assert shifts["end_s"].max() == 3900


def test_repeated_shifts_are_dropped_once() -> None:
    rows = chart_rows(trimmed("shiftcharts", 2022020041))
    shifts, drops = parse_trimmed(2022020041)
    distinct = {(r["playerId"], r["period"], r["startTime"], r["endTime"]) for r in rows}
    numbers = {(r["playerId"], r["period"], r["shiftNumber"]) for r in rows}
    assert len(distinct) < len(numbers)  # some repeats come under a new shift number
    assert drops == ShiftDrops(dropped=len(rows) - len(distinct)) != ShiftDrops()
    assert shifts.height == len(distinct)


def shift(**changes: Any) -> dict[str, Any]:
    row = {
        "typeCode": SHIFT,
        "gameId": 2019020003,
        "teamId": 22,
        "playerId": 8478402,
        "period": 1,
        "shiftNumber": 1,
        "startTime": "00:00",
        "endTime": "00:45",
        "duration": "00:45",
    }
    return row | changes


@pytest.mark.parametrize(
    "changes",
    [
        {"endTime": "20:30"},  # past the end of the period
        {"period": 4, "startTime": "04:30", "endTime": "05:10"},  # overtime lasts 5 minutes
        {"startTime": "10:00", "endTime": "09:00"},
        {"startTime": ""},
        {"endTime": "", "duration": "00:40"},  # no end, yet a duration
        {"playerId": None},
        {"shiftNumber": None},
        {"period": 0},
        {"period": None},
        {"period": -1},
    ],
)
def test_malformed_rows_are_bad(changes: dict[str, Any]) -> None:
    rows, drops = shift_rows([shift(**changes)], TRIMMED_GAMES[2019020003], "k")
    assert (rows, drops) == ([], ShiftDrops(bad=1))


def test_a_shift_number_repeated_with_other_times_is_bad() -> None:
    game = TRIMMED_GAMES[2019020003]
    rows, drops = shift_rows(
        [shift(), shift(startTime="01:30", endTime="02:10"), shift()], game, "k"
    )
    assert [(r["start_s"], r["end_s"]) for r in rows] == [(0, 45)]
    assert drops == ShiftDrops(dropped=1, bad=1)


def test_a_shift_repeated_under_the_next_numbers_is_dropped() -> None:
    # 2023020022: shifts 6, 7 and 8 of one NYR defenseman are all 02:55 to 04:16 of the second
    # period, which added 162 seconds to his time on ice.
    rows, drops = shift_rows(
        [shift(shiftNumber=6), shift(shiftNumber=7), shift(shiftNumber=8)],
        TRIMMED_GAMES[2019020003],
        "k",
    )
    assert [r["shift_number"] for r in rows] == [6]
    assert drops == ShiftDrops(dropped=2)


def test_a_row_of_another_game_is_foreign() -> None:
    rows, drops = shift_rows([shift(gameId=2019020004)], TRIMMED_GAMES[2019020003], "k")
    assert (rows, drops) == ([], ShiftDrops(foreign=1))
