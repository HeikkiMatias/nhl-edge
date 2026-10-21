"""Point-in-time rules for faceoffs (#96). A game's play-by-play counts as public at 10:00 UTC the
morning after its game date (ADR 0003, ADR 0004), so no prediction for a game ever sees that game's
own faceoffs. The player layer reads them only for earlier games: the faceoff that opens a stint
gives RAPM its zone start (#97, #101).

The column set is locked here."""

import pytest
from feed_fixtures import MTL_ARI, OPENING_WEEK_GAMES, assert_public_the_morning_after, parsed_feeds

from nhl_edge.lake.schemas import Faceoffs, dtypes

GAMES = (*OPENING_WEEK_GAMES, MTL_ARI)

COLUMNS = [
    "game_id",
    "season",
    "game_date",
    "event_id",
    "sort_order",
    "period",
    "seconds",
    "winning_team",
    "home_won",
    "winner_id",
    "loser_id",
    "zone",
    "observed_utc",
    "raw_key",
]


@pytest.mark.parametrize("game_id", GAMES)
def test_a_game_never_sees_its_own_faceoffs(game_id: int) -> None:
    assert_public_the_morning_after(parsed_feeds(game_id)["faceoffs"], game_id)


def test_faceoff_columns_are_locked() -> None:
    assert list(dtypes(Faceoffs)) == COLUMNS
