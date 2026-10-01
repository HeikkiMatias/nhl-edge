"""Point-in-time rules for penalties (#96). A game's play-by-play counts as public at 10:00 UTC the
morning after its game date (ADR 0003, ADR 0004), so no prediction for a game ever sees that game's
own penalties, even one made hours after the puck dropped. The player layer reads them only for
earlier games: each player's penalties taken and drawn give B3's expected power plays.

The column set is locked here: scoring credits and the penalty's position on the ice stay out."""

import pytest
from feed_fixtures import MTL_ARI, OPENING_WEEK_GAMES, assert_public_the_morning_after, parsed_feeds

from nhl_edge.lake.schemas import Penalties, dtypes

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
    "committed_by",
    "drawn_by",
    "served_by",
    "type_code",
    "desc_key",
    "duration_min",
    "observed_utc",
    "raw_key",
]


@pytest.mark.parametrize("game_id", GAMES)
def test_a_game_never_sees_its_own_penalties(game_id: int) -> None:
    assert_public_the_morning_after(parsed_feeds(game_id)["penalties"], game_id)


def test_penalty_columns_are_locked() -> None:
    assert list(dtypes(Penalties)) == COLUMNS
