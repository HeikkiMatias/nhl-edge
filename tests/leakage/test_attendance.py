"""Point-in-time rules for attendance limits. A game's capacity share joins its schedule row,
public a day before the game (ADR 0005), so a limit counts only once it was announced before the
game's date. Until then the regime it replaced still reads as current. A limit's end date is only
used once the next limit is known or, for a lifted limit, from the day it was lifted: every lift
in the file was announced at least a day ahead."""

from dataclasses import replace
from datetime import date

import pandera.errors
import polars as pl
import pytest

from nhl_edge.lake.schemas import AttendanceLimits
from nhl_edge.reference import Reference, capacity_share

REF = Reference.load()
ARENA, VENUE = "ball_arena", "Ball Arena"


def limits(*rows: tuple[date, date, float, date | None]) -> Reference:
    """REF with ball_arena's limits replaced by rows of (first, last, share, announced)."""
    frame = pl.DataFrame(
        {
            "arena_id": [ARENA] * len(rows),
            "first_date": [row[0] for row in rows],
            "last_date": [row[1] for row in rows],
            "capacity_share": [row[2] for row in rows],
            "limit": ["test"] * len(rows),
            "announced": pl.Series([row[3] for row in rows], dtype=pl.Date),
            "source": ["https://example.org"] * len(rows),
        }
    )
    others = REF.attendance_limits.filter(pl.col("arena_id") != ARENA)
    return replace(REF, attendance_limits=AttendanceLimits.validate(pl.concat([others, frame])))


def share_on(ref: Reference, *days: date) -> list[float]:
    games = pl.DataFrame(
        {"game_id": range(len(days)), "venue": [VENUE] * len(days), "game_date": list(days)}
    )
    return capacity_share(games, ref).sort("game_id")["capacity_share"].to_list()


def test_an_increase_counts_from_the_day_after_its_announcement() -> None:
    # Empty from the start; a 50% limit from March 9, announced on March 9 itself.
    ref = limits(
        (date(2021, 1, 13), date(2021, 3, 8), 0.0, None),
        (date(2021, 3, 9), date(2021, 5, 19), 0.5, date(2021, 3, 9)),
    )
    assert share_on(ref, date(2021, 3, 8), date(2021, 3, 9), date(2021, 3, 10)) == [0, 0, 0.5]


def test_a_cut_announced_on_game_day_is_not_known_that_day() -> None:
    # Montreal's arena was closed at public health's request on the day of the game.
    ref = limits((date(2021, 12, 16), date(2022, 2, 20), 0.0, date(2021, 12, 16)))
    assert share_on(ref, date(2021, 12, 15), date(2021, 12, 16), date(2021, 12, 17)) == [1, 1, 0]
    montreal = pl.DataFrame(
        {"game_id": [1], "venue": ["Centre Bell"], "game_date": [date(2021, 12, 16)]}
    )
    assert capacity_share(montreal, REF)["capacity_share"].to_list() == [1.0]


def test_a_limit_announced_ahead_counts_from_its_first_day() -> None:
    ref = limits((date(2021, 12, 20), date(2022, 2, 15), 0.5, date(2021, 12, 17)))
    assert share_on(ref, date(2021, 12, 19), date(2021, 12, 20), date(2022, 2, 16)) == [1, 0.5, 1]


def test_a_share_depends_only_on_the_games_own_arena_and_date() -> None:
    # Adding or dropping other games never changes a game's share.
    alone = share_on(REF, date(2021, 4, 2))
    among_others = share_on(REF, date(2021, 3, 1), date(2021, 4, 2), date(2021, 5, 13))
    assert alone == among_others[1:2] == [0.22]


def test_a_limit_reported_only_after_it_ended_is_rejected() -> None:
    with pytest.raises(pandera.errors.SchemaError):
        limits((date(2021, 1, 13), date(2021, 3, 8), 0.0, date(2021, 3, 9)))
