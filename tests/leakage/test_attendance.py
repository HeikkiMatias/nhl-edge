"""Point-in-time rules for attendance limits. capacity_share gives a game the share of seats open
as known at the prediction time. It reads only the schedule rows schedule_known_at returns, so a
game's venue and date stay hidden until its schedule is public (ADR 0005), and an upcoming game
other than the ones predicted never shows. A limit's source counts as public from 10:00 UTC the
day after its date, the rule ADR 0003 sets for results. So a limit reported on game day is not
known that morning. Until it is, the limit it replaced still reads as current. A limit's end
counts only once what ended it is known: the next limit's announcement, or the lift's
(ended_announced)."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

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


def schedule(*games: tuple[int, str, date]) -> pl.DataFrame:
    """Schedule rows as (game_id, venue, date), each public at 00:00 UTC the day before."""
    return pl.DataFrame(
        {
            "game_id": [game_id for game_id, _, _ in games],
            "venue": [venue for _, venue, _ in games],
            "game_date": [day for _, _, day in games],
            "observed_utc": [
                datetime.combine(day, time(0), tzinfo=UTC) - timedelta(days=1)
                for _, _, day in games
            ],
        }
    )


def share(ref: Reference, game_date: date, prediction_utc: datetime) -> float:
    games = schedule((1, VENUE, game_date))
    return capacity_share(games, prediction_utc, [1], ref)["capacity_share"].item()


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
    montreal = schedule((1, "Centre Bell", date(2021, 12, 16)))
    before = capacity_share(montreal, morning(date(2021, 12, 16)), [1], REF)
    assert before["capacity_share"].item() == 1.0


def test_a_day_before_prediction_sees_less_than_a_game_day_one() -> None:
    # Tampa Bay's increase to 4,200 fans was announced on May 5, the day of a game.
    # The day before, at 09:00 UTC, the May 5 report is not public yet (10:00 UTC on May 6).
    tampa = schedule((1, "Amalie Arena", date(2021, 5, 7)))
    day_before = capacity_share(tampa, datetime(2021, 5, 6, 9, 0, tzinfo=UTC), [1], REF)
    game_day = capacity_share(tampa, morning(date(2021, 5, 7)), [1], REF)
    shares = (day_before["capacity_share"].item(), game_day["capacity_share"].item())
    assert shares == (round(3_800 / 19_092, 3), round(4_200 / 19_092, 3))


def test_a_lift_counts_only_once_announced() -> None:
    # 50% until February 16, lifted from February 17, lift announced February 15.
    ref = limits(
        (date(2021, 12, 20), date(2022, 2, 16), 0.5, date(2021, 12, 17), date(2022, 2, 15))
    )
    # The lift's report is public from 10:00 UTC on February 16.
    february_17 = date(2022, 2, 17)
    assert share(ref, february_17, datetime(2022, 2, 16, 9, 0, tzinfo=UTC)) == 0.5
    assert share(ref, february_17, morning(date(2022, 2, 16))) == 1.0


def test_a_limit_ends_only_once_its_successor_is_known() -> None:
    # A cut to 0 from February 1, reported January 31, is replaced by 50% from February 10,
    # reported on February 12.
    ref = limits(
        (date(2022, 2, 1), date(2022, 2, 9), 0.0, date(2022, 1, 31), None),
        (date(2022, 2, 10), date(2022, 2, 28), 0.5, date(2022, 2, 12), date(2022, 2, 20)),
    )
    february_11 = date(2022, 2, 11)
    assert share(ref, february_11, morning(february_11)) == 0.0
    assert share(ref, date(2022, 2, 14), morning(date(2022, 2, 14))) == 0.5
    # Before the cut's report was public (10:00 UTC on February 1), nothing was known: full.
    february_1 = date(2022, 2, 1)
    assert share(ref, february_1, datetime(2022, 2, 1, 9, 0, tzinfo=UTC)) == 1.0
    assert share(ref, february_1, morning(february_1)) == 0.0


def test_a_share_depends_only_on_the_games_own_arena_and_date() -> None:
    # Adding other games never changes a game's share.
    t = morning(date(2021, 4, 2))
    alone = schedule((2, VENUE, date(2021, 4, 2)))
    many = schedule(
        (1, VENUE, date(2021, 3, 1)),
        (2, VENUE, date(2021, 4, 2)),
        (3, "TD Garden", date(2021, 4, 2)),
    )
    one = capacity_share(alone, t, [2], REF)["capacity_share"].to_list()
    among = capacity_share(many, t, [2, 3], REF).filter(pl.col("game_id") == 2)["capacity_share"]
    assert one == among.to_list() == [0.22]


def test_a_game_gets_a_share_only_once_its_schedule_is_public() -> None:
    # The schedule is public at 00:00 UTC the day before: a prediction earlier than that sees no
    # venue or date for the game, so it gets no share.
    games = schedule((1, VENUE, date(2021, 4, 2)))
    assert capacity_share(games, datetime(2021, 3, 31, 23, 0, tzinfo=UTC), [1], REF).is_empty()
    assert capacity_share(games, morning(date(2021, 4, 2)), [1], REF).height == 1


def test_upcoming_games_not_predicted_stay_hidden() -> None:
    # A played game with a public result shows; tomorrow's game, not predicted, does not: a row
    # for it would say it went on to be played.
    games = schedule(
        (1, VENUE, date(2021, 3, 30)), (2, VENUE, date(2021, 4, 2)), (3, VENUE, date(2021, 4, 3))
    )
    known = capacity_share(games, morning(date(2021, 4, 2)), [2], REF)
    assert known["game_id"].to_list() == [1, 2]


def test_the_schema_holds_the_announcement_rules() -> None:
    # A limit reported only after it ended would never apply.
    with pytest.raises(pandera.errors.SchemaError):
        limits((date(2021, 1, 13), date(2021, 3, 8), 0.0, date(2021, 3, 9), None))
    # Only a limit in force from a limited season's first day may go without a date.
    with pytest.raises(pandera.errors.SchemaError):
        limits((date(2022, 1, 5), date(2022, 1, 30), 0.0, None, None))
