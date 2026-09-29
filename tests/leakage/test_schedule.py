"""Point-in-time rules for the schedule (ADR 0005). A game's pre-game facts (teams, start, venue,
flags) count as public a day before it starts, and its result the morning after (ADR 0003). So on
game day a prediction sees who plays where, never how it ended, and the schedule table has no
result column at all to leak.

The table holds only games that went on to be played, so its rows for games not yet started say
something about the future: a game postponed at short notice is missing. Features therefore read
it through schedule_known_at, which returns games already started plus the games being
predicted, never other upcoming ones."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandera.errors
import polars as pl
import pytest

from nhl_edge.ingest.games import (
    listed_games,
    parse_games,
    results_known_at,
    schedule_known_at,
    schedule_of,
)
from nhl_edge.lake.schemas import SCHEDULE_LEAD, Schedule, dtypes
from nhl_edge.lake.tables import known_at

FIXTURES = Path(__file__).parents[1] / "unit" / "fixtures" / "nhl_api"
OPENING = parse_games(
    listed_games(
        (FIXTURES / "schedule_2010-10-07.json").read_bytes(), {date(2010, 10, 7), date(2010, 10, 8)}
    ),
    "nhl/schedule/x",
)
SCHEDULE = schedule_of(OPENING)
# CAR at MIN in Helsinki starts at 16:00 UTC on 2010-10-07, CHI at COL at 02:00 UTC the next
# night (the same NHL game date), and MIN at CAR in Helsinki at 16:00 UTC on 2010-10-08.
HELSINKI, CHI_AT_COL, NEXT_DAY = 2010020003, 2010020004, 2010020008
# The game-day odds slot and prediction runs come from 11:05 UTC on.
MORNING = datetime(2010, 10, 7, 11, 5, tzinfo=UTC)
SECOND = timedelta(seconds=1)

COLUMNS = [
    "game_id",
    "season",
    "game_date",
    "start_utc",
    "home",
    "away",
    "venue",
    "neutral_site",
    "limited_attendance",
    "observed_utc",
    "raw_key",
]


def test_the_schedule_holds_no_result() -> None:
    assert list(dtypes(Schedule)) == COLUMNS


def test_every_game_is_public_exactly_a_day_before_it_starts() -> None:
    assert (SCHEDULE["observed_utc"] == SCHEDULE["start_utc"] - SCHEDULE_LEAD).all()
    for game in SCHEDULE.iter_rows(named=True):
        public = game["observed_utc"]
        assert game["game_id"] not in known_at(SCHEDULE, public)["game_id"]
        assert game["game_id"] in known_at(SCHEDULE, public + SECOND)["game_id"]
        # Games that have started by then come along; the predicted game only once public.
        predicting = [game["game_id"]]
        assert game["game_id"] not in schedule_known_at(SCHEDULE, public, predicting)["game_id"]
        assert (
            game["game_id"] in schedule_known_at(SCHEDULE, public + SECOND, predicting)["game_id"]
        )


def test_on_game_day_the_schedule_is_known_and_the_result_is_not() -> None:
    slate = [HELSINKI, CHI_AT_COL]
    assert schedule_known_at(SCHEDULE, MORNING, slate)["game_id"].to_list() == slate
    assert results_known_at(OPENING, MORNING).is_empty()


def test_other_games_appear_only_once_their_result_is_public() -> None:
    # Predicting Helsinki in the morning: the late game that night and the next day's game are
    # public, but whether they were played is not known yet.
    assert schedule_known_at(SCHEDULE, MORNING, [HELSINKI])["game_id"].to_list() == [HELSINKI]
    # Helsinki is under way by the evening, but a game past its scheduled start may still be
    # delayed or called off: it shows only once its result is public, the next morning.
    evening = datetime(2010, 10, 7, 18, 0, tzinfo=UTC)
    assert schedule_known_at(SCHEDULE, evening, [CHI_AT_COL])["game_id"].to_list() == [CHI_AT_COL]
    next_morning = datetime(2010, 10, 8, 11, 5, tzinfo=UTC)
    known = schedule_known_at(SCHEDULE, next_morning, [NEXT_DAY])["game_id"].to_list()
    assert known == [HELSINKI, CHI_AT_COL, NEXT_DAY]


TAHOE = parse_games(
    listed_games(
        (FIXTURES / "schedule_2021-02-20.json").read_bytes(), {date(2021, 2, 20), date(2021, 2, 21)}
    ),
    "nhl/schedule/x",
)
# VGK at COL, Lake Tahoe: scheduled for 20:00 UTC on 2021-02-20, suspended for sun on the ice and
# finished at about 07:00 UTC the next day. PHI at BOS the next day was moved from 15:00 to 19:30
# ET at short notice.
DELAYED, MOVED = 2020020287, 2020020290


def test_a_delayed_game_does_not_show_before_its_result() -> None:
    # Hours past its scheduled start, the suspended game was still undecided: nothing may show
    # whether it would be finished.
    schedule = schedule_of(TAHOE)
    suspended = datetime(2021, 2, 21, 2, 0, tzinfo=UTC)
    assert DELAYED not in schedule_known_at(schedule, suspended, [MOVED])["game_id"]
    public = datetime(2021, 2, 22, 10, 0, tzinfo=UTC)
    assert DELAYED in schedule_known_at(schedule, public + SECOND, [MOVED])["game_id"]


def test_a_game_moved_at_short_notice_is_public_only_by_its_original_start() -> None:
    # The move to 19:30 ET was public by the original 15:00 ET (20:00 UTC) puck drop at the latest,
    # later than a day before the new start.
    schedule = schedule_of(TAHOE)
    moved = schedule.filter(pl.col("game_id") == MOVED).row(0, named=True)
    assert moved["start_utc"] == datetime(2021, 2, 22, 0, 30, tzinfo=UTC)
    original_start = datetime(2021, 2, 21, 20, 0, tzinfo=UTC)
    assert moved["observed_utc"] == original_start
    assert MOVED not in schedule_known_at(schedule, original_start, [MOVED])["game_id"]
    assert MOVED in schedule_known_at(schedule, original_start + SECOND, [MOVED])["game_id"]


def test_a_schedule_public_sooner_than_a_day_ahead_is_rejected() -> None:
    game = pl.col("game_id") == HELSINKI
    for shift, valid in (
        (-SECOND, False),  # earlier than the rule: a leak
        (timedelta(hours=12), True),  # later, an override for a re-dated game
        (SCHEDULE_LEAD, False),  # at the start itself
    ):
        moved = SCHEDULE.with_columns(
            pl.when(game).then(pl.col("observed_utc") + shift).otherwise("observed_utc")
        )
        if valid:
            Schedule.validate(moved)
        else:
            with pytest.raises(pandera.errors.SchemaError):
                Schedule.validate(moved)
