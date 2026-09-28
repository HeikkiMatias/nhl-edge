"""Point-in-time rules for game results. The NHL API has no end-of-game time, so a result counts as
public at 10:00 UTC the morning after its game date (ADR 0003). A prediction never sees its own
game's result, nor one that may still be under way, even when the game ran hours late."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from nhl_edge.ingest.games import listed_games, parse_games, results_known_at
from nhl_edge.lake.schemas import MIN_RESULT_LAG

FIXTURES = Path(__file__).parents[1] / "unit" / "fixtures" / "nhl_api"


def games_in(name: str, *days: date) -> pl.DataFrame:
    return parse_games(listed_games((FIXTURES / name).read_bytes(), set(days)), "nhl/schedule/x")


OPENING = games_in("schedule_2010-10-07.json", date(2010, 10, 7), date(2010, 10, 8))
TAHOE = games_in("schedule_2021-02-20.json", date(2021, 2, 20), date(2021, 2, 21))


def test_a_game_never_sees_its_own_result() -> None:
    for games in (OPENING, TAHOE):
        for game in games.iter_rows(named=True):
            for moment in (game["start_utc"], game["start_utc"] + MIN_RESULT_LAG):
                assert game["game_id"] not in results_known_at(games, moment)["game_id"]


def test_results_are_known_only_from_the_morning_after() -> None:
    # Helsinki, 2010-10-07 at 16:00 UTC: over by about 18:30, public at 10:00 the next day.
    morning = datetime(2010, 10, 8, 10, 0, tzinfo=UTC)
    assert 2010020003 not in results_known_at(OPENING, morning)["game_id"]
    assert 2010020003 in results_known_at(OPENING, morning + timedelta(seconds=1))["game_id"]


def test_a_game_suspended_for_hours_is_not_known_before_it_ends() -> None:
    # VGK at COL, Lake Tahoe: scheduled 20:00 UTC, suspended after the first period for sun on the
    # ice, resumed at 9 pm PT and finished at about 07:00 UTC the next day. Start plus six hours
    # would have leaked its score into predictions from 02:00 UTC.
    game = TAHOE.filter(pl.col("game_id") == 2020020287).row(0, named=True)
    assert game["start_utc"] == datetime(2021, 2, 20, 20, 0, tzinfo=UTC)
    ended_by = datetime(2021, 2, 21, 8, 0, tzinfo=UTC)
    assert game["observed_utc"] > ended_by
    assert results_known_at(TAHOE, ended_by).is_empty()


def test_every_result_is_observed_at_least_six_hours_after_its_start() -> None:
    for games in (OPENING, TAHOE):
        assert (games["observed_utc"] >= games["start_utc"] + MIN_RESULT_LAG).all()
