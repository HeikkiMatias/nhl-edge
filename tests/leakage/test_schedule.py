"""Point-in-time rules for the schedule (ADR 0005). A game's pre-game facts (teams, start, venue,
flags) count as public a day before it starts, and its result the morning after (ADR 0003). So on
game day a prediction sees who plays where, never how it ended, and the schedule table has no
result column at all to leak."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from nhl_edge.ingest.games import listed_games, parse_games, results_known_at, schedule_of
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
# The game-day odds slot and prediction run come after 11:05 UTC.
MORNING_SLOT = timedelta(hours=11, minutes=5)

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


def test_every_game_is_public_a_day_before_it_starts() -> None:
    assert (SCHEDULE["observed_utc"] + SCHEDULE_LEAD <= SCHEDULE["start_utc"]).all()
    for game in SCHEDULE.iter_rows(named=True):
        assert game["game_id"] not in known_at(SCHEDULE, game["observed_utc"])["game_id"]


def test_on_game_day_the_schedule_is_known_and_the_result_is_not() -> None:
    # CAR at MIN in Helsinki starts at 16:00 UTC, and CHI at COL at 02:00 UTC the next day. Both
    # are on the 2010-10-07 slate.
    morning = datetime(2010, 10, 7, tzinfo=UTC) + MORNING_SLOT
    slate = OPENING.filter(pl.col("game_date") == date(2010, 10, 7))["game_id"].to_list()
    assert slate == [2010020003, 2010020004]
    for game_id in slate:
        assert game_id in known_at(SCHEDULE, morning)["game_id"]
        assert game_id not in results_known_at(OPENING, morning)["game_id"]
