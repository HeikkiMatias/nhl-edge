"""Point-in-time rules for blocked shots (#419), as for shots: a game's play-by-play counts as
public at 10:00 UTC the morning after its game date (ADR 0003, ADR 0004), so no prediction for a
game ever sees that game's own blocked shots, even one made hours after the puck dropped. The
column set is locked here: who took and blocked each attempt, never a scoring credit."""

import pytest
from feed_fixtures import MTL_ARI, OPENING_WEEK_GAMES, assert_public_the_morning_after, parsed_feeds

from nhl_edge.lake.schemas import BlockedShots, dtypes

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
    "shooter_id",
    "blocker_id",
    "reason",
    "zone",
    "x",
    "y",
    "skaters_for",
    "skaters_against",
    "strength",
    "is_empty_net",
    "situation_code",
    "strength_source",
    "observed_utc",
    "raw_key",
]


@pytest.mark.parametrize("game_id", GAMES)
def test_a_game_never_sees_its_own_blocked_shots(game_id: int) -> None:
    assert_public_the_morning_after(parsed_feeds(game_id)["blocked_shots"], game_id)


def test_blocked_shots_hold_no_scoring_credits() -> None:
    assert list(dtypes(BlockedShots)) == COLUMNS
