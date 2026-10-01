"""Point-in-time rules for shots. A game's play-by-play counts as public at 10:00 UTC the morning
after its game date, like its result (ADR 0003, ADR 0004): the live pipeline fetches it with the
result, so no prediction for a game ever sees that game's own shots, even one made hours after the
puck dropped.

A backfilled play-by-play includes post-game corrections. Assists and other scoring credits stay
out, and the column set is locked here. Corrected values in the kept columns (a goal's scorer, a
fixed shot record) are a small look-ahead that ADR 0004 accepts and #30 measures."""

import pytest
from feed_fixtures import MTL_ARI, OPENING_WEEK_GAMES, assert_public_the_morning_after, parsed_feeds

from nhl_edge.lake.schemas import Shots, dtypes

GAMES = (*OPENING_WEEK_GAMES, MTL_ARI)

COLUMNS = [
    "game_id",
    "season",
    "game_date",
    "event_id",
    "sort_order",
    "period",
    "seconds",
    "team",
    "is_home",
    "event_type",
    "is_goal",
    "shooter_id",
    "goalie_id",
    "shot_type",
    "zone",
    "x",
    "y",
    "skaters_for",
    "skaters_against",
    "strength",
    "is_empty_net",
    "is_penalty_shot",
    "situation_code",
    "strength_source",
    "prev_event_type",
    "prev_seconds",
    "prev_by_shooting_team",
    "prev_zone",
    "observed_utc",
    "raw_key",
]


@pytest.mark.parametrize("game_id", GAMES)
def test_a_game_never_sees_its_own_shots(game_id: int) -> None:
    assert_public_the_morning_after(parsed_feeds(game_id)["shots"], game_id)


def test_shots_hold_no_scoring_credits() -> None:
    assert list(dtypes(Shots)) == COLUMNS
