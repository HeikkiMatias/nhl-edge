"""Point-in-time rules for attendance limits. capacity_share gives a game the share of seats open
as known at the prediction time. A limit's source counts as public from 10:00 UTC the day after
its date, the rule ADR 0003 sets for results. So a limit reported on game day is not known that
morning. Until it is, the limit it replaced still reads as current. A limit's end counts only once
what ended it is known: the next limit's announcement, or the lift's (ended_announced)."""

from dataclasses import replace
from datetime import UTC, date, datetime

import pandera.errors
import polars as pl
import pytest

from nhl_edge.lake.schemas import AttendanceLimits
from nhl_edge.reference import Reference, capacity_share

REF = Reference.load()
ARENA, VENUE = "ball_arena", "Ball Arena"
URL = "https://example.org"


def limits(*rows: tuple[date, date, float, date | None, date | None]) -> Reference:
    """REF with ball_arena's limits replaced by (first, last, share, announced, lift announced)."""
    frame = pl.DataFrame(
        {
            "arena_id": [ARENA] * len(rows),
            "first_date": [row[0] for row in rows],
            "last_date": [row[1] for row in rows],
            "capacity_share": [row[2] for row in rows],
            "limit": ["test"] * len(rows),
            "announced": pl.Series([row[3] for row in rows], dtype=pl.Date),
            "source": [URL] * len(rows),
            "ended_announced": pl.Series([row[4] for row in rows], dtype=pl.Date),
            "ended_source": [URL if row[4] else None for row in rows],
        }
    )
    others = REF.attendance_limits.filter(pl.col("arena_id") != ARENA)
    return replace(REF, attendance_limits=AttendanceLimits.validate(pl.concat([others, frame])))


def morning(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 11, 5, tzinfo=UTC)


def share(ref: Reference, game_date: date, prediction_utc: datetime) -> float:
    games = pl.DataFrame({"game_id": [1], "venue": [VENUE], "game_date": [game_date]})
    return capacity_share(games, prediction_utc, ref)["capacity_share"].item()


def test_a_limit_counts_from_the_morning_after_its_announcement() -> None:
    # Empty from the season's start; 50% from March 9, announced on March 9 itself.
    ref = limits(
        (date(2021, 1, 13), date(2021, 3, 8), 0.0, None, None),
        (date(2021, 3, 9), date(2021, 5, 19), 0.5, date(2021, 3, 9), None),
    )
    march_9, march_10 = date(2021, 3, 9), date(2021, 3, 10)
    assert share(ref, march_9, morning(march_9)) == 0.0
    assert share(ref, march_10, morning(march_10)) == 0.5
    # At 10:00 UTC the day after the announcement exactly, it is not public yet.
    assert share(ref, march_10, datetime(2021, 3, 10, 10, 0, tzinfo=UTC)) == 0.0


def test_a_cut_announced_on_game_day_is_not_known_that_morning() -> None:
    # Montreal's arena was closed at public health's request on the day of the game.
    montreal = pl.DataFrame(
        {"game_id": [1], "venue": ["Centre Bell"], "game_date": [date(2021, 12, 16)]}
    )
    before = capacity_share(montreal, morning(date(2021, 12, 16)), REF)
    assert before["capacity_share"].item() == 1.0


def test_a_day_before_prediction_sees_less_than_a_game_day_one() -> None:
    # Tampa Bay's increase to 4,200 fans was announced on May 5, the day of a game.
    tampa = pl.DataFrame(
        {"game_id": [1], "venue": ["Amalie Arena"], "game_date": [date(2021, 5, 7)]}
    )
    day_before = capacity_share(tampa, morning(date(2021, 5, 5)), REF)["capacity_share"].item()
    game_day = capacity_share(tampa, morning(date(2021, 5, 7)), REF)["capacity_share"].item()
    assert (day_before, game_day) == (round(3_800 / 19_092, 3), round(4_200 / 19_092, 3))


def test_a_lift_counts_only_once_announced() -> None:
    # 50% until February 16, lifted from February 17, lift announced February 15.
    ref = limits(
        (date(2021, 12, 20), date(2022, 2, 16), 0.5, date(2021, 12, 17), date(2022, 2, 15))
    )
    february_17 = date(2022, 2, 17)
    assert share(ref, february_17, morning(date(2022, 2, 15))) == 0.5
    assert share(ref, february_17, morning(date(2022, 2, 16))) == 1.0


def test_a_limit_ends_only_once_its_successor_is_known() -> None:
    # A cut to 0 from February 1 is replaced by 50% from February 10, reported on February 12.
    ref = limits(
        (date(2022, 2, 1), date(2022, 2, 9), 0.0, date(2022, 1, 28), None),
        (date(2022, 2, 10), date(2022, 2, 28), 0.5, date(2022, 2, 12), date(2022, 2, 20)),
    )
    february_11 = date(2022, 2, 11)
    assert share(ref, february_11, morning(february_11)) == 0.0
    assert share(ref, date(2022, 2, 14), morning(date(2022, 2, 14))) == 0.5
    # Before the cut was announced, nothing was known: full.
    assert share(ref, date(2022, 2, 2), morning(date(2022, 1, 28))) == 1.0


def test_a_share_depends_only_on_the_games_own_arena_and_date() -> None:
    # Adding other games never changes a game's share.
    t = morning(date(2021, 4, 2))
    alone = pl.DataFrame({"game_id": [2], "venue": [VENUE], "game_date": [date(2021, 4, 2)]})
    many = pl.DataFrame(
        {
            "game_id": [1, 2, 3],
            "venue": [VENUE, VENUE, "TD Garden"],
            "game_date": [date(2021, 3, 1), date(2021, 4, 2), date(2021, 4, 2)],
        }
    )
    one = capacity_share(alone, t, REF)["capacity_share"].to_list()
    among = capacity_share(many, t, REF).filter(pl.col("game_id") == 2)["capacity_share"]
    assert one == among.to_list() == [0.22]


def test_the_schema_holds_the_announcement_rules() -> None:
    # A limit reported only after it ended would never apply.
    with pytest.raises(pandera.errors.SchemaError):
        limits((date(2021, 1, 13), date(2021, 3, 8), 0.0, date(2021, 3, 9), None))
    # Only a limit in force from a limited season's first day may go without a date.
    with pytest.raises(pandera.errors.SchemaError):
        limits((date(2022, 1, 5), date(2022, 1, 30), 0.0, None, None))
