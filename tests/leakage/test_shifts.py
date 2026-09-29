"""Point-in-time rules for shifts. A game's shift chart counts as public at 10:00 UTC the morning
after its game date, like its result (ADR 0003, ADR 0004), so no prediction for a game sees who was
on the ice in it."""

import pytest
from feed_fixtures import MTL_ARI, OPENING_WEEK_GAMES, assert_public_the_morning_after, parsed_feeds


@pytest.mark.parametrize("game_id", (*OPENING_WEEK_GAMES, MTL_ARI))
def test_a_game_never_sees_its_own_shifts(game_id: int) -> None:
    assert_public_the_morning_after(parsed_feeds(game_id)["shifts"], game_id)
