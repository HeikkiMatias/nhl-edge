"""Point-in-time rules for game results: a result counts as public six hours after the scheduled
start (ADR 0003), so a prediction never sees its own game's result, nor one still being played."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from nhl_edge.ingest.games import RESULT_LAG, listed_games, parse_games, results_known_at

WEEK = Path(__file__).parents[1] / "unit" / "fixtures" / "nhl_api" / "schedule_2010-10-07.json"
GAMES = parse_games(
    listed_games(WEEK.read_bytes(), {date(2010, 10, 7), date(2010, 10, 8)}), "nhl/schedule/test"
)


def test_a_game_never_sees_its_own_result() -> None:
    for game in GAMES.iter_rows(named=True):
        for moment in (game["start_utc"], game["start_utc"] + RESULT_LAG):
            known = results_known_at(GAMES, moment)
            assert game["game_id"] not in known["game_id"].to_list()


def test_results_are_known_only_after_the_lag() -> None:
    helsinki_start = datetime(2010, 10, 7, 16, 0, tzinfo=UTC)
    during = results_known_at(GAMES, helsinki_start + timedelta(hours=5, minutes=59))
    after = results_known_at(GAMES, helsinki_start + RESULT_LAG + timedelta(seconds=1))
    assert during.is_empty()
    assert after["game_id"].to_list() == [2010020003]


def test_every_result_is_observed_after_its_start() -> None:
    assert (GAMES["observed_utc"] >= GAMES["start_utc"] + RESULT_LAG).all()
    assert GAMES.filter(pl.col("observed_utc") <= pl.col("start_utc")).is_empty()
