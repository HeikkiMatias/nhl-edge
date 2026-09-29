"""Point-in-time rules for shift coverage. A game's coverage row is built from its own shift chart,
play-by-play and boxscore, so it counts as public when they do: 10:00 UTC the morning after its game
date (ADR 0003, ADR 0004). A filter on coverage may only drop games that finished before the
prediction."""

import pytest
from feed_fixtures import MTL_ARI, OPENING_WEEK_GAMES, assert_public_the_morning_after, parsed_feeds


@pytest.mark.parametrize("game_id", (*OPENING_WEEK_GAMES, MTL_ARI))
def test_a_game_never_sees_its_own_coverage(game_id: int) -> None:
    assert_public_the_morning_after(parsed_feeds(game_id)["shift_coverage"], game_id)
