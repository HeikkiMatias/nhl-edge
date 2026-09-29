"""Point-in-time rules for pre-game goalies: a prediction may use only polls observed before it,
and a starter confirmed after the prediction time is invisible to it."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from nhl_edge.ingest.pregame import parse_pregame_goalies
from nhl_edge.lake.tables import known_at

BOXSCORE = (
    Path(__file__).parents[1] / "unit" / "fixtures" / "nhl_api" / "pregame-boxscore_2026020001.json"
).read_bytes()
START = datetime(2026, 9, 29, 21, 0, tzinfo=UTC)
MORNING = datetime(2026, 9, 29, 11, 5, tzinfo=UTC)
PRE_GAME = START - timedelta(minutes=15)


def confirmed() -> bytes:
    data = json.loads(BOXSCORE)
    data["playerByGameStats"] = {
        "homeTeam": {"goalies": [{"playerId": 8483548, "starter": True}]},
        "awayTeam": {"goalies": [{"playerId": 8474593, "starter": True}]},
    }
    return json.dumps(data).encode()


def polls() -> pl.DataFrame:
    return pl.concat(
        [
            parse_pregame_goalies(BOXSCORE, MORNING, "morning"),
            parse_pregame_goalies(confirmed(), PRE_GAME, "pre"),
        ]
    )


def test_polls_at_or_after_the_prediction_time_are_excluded() -> None:
    usable = known_at(polls(), PRE_GAME)
    assert usable.height == 2
    assert (usable["observed_utc"] < PRE_GAME).all()
    assert known_at(polls(), MORNING).is_empty()


def test_a_starter_confirmed_after_the_prediction_time_is_not_seen() -> None:
    before = known_at(polls(), PRE_GAME - timedelta(minutes=1))
    assert before["starter_id"].null_count() == before.height
    after = known_at(polls(), PRE_GAME + timedelta(minutes=1))
    assert after["starter_id"].drop_nulls().len() == 2


def test_no_row_is_observed_at_or_after_the_start() -> None:
    assert parse_pregame_goalies(confirmed(), START, "live").is_empty()
    assert (polls()["observed_utc"] < polls()["start_utc"]).all()
