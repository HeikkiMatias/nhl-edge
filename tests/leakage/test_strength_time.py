"""Point-in-time rules for strength time (#72). A game's seconds at each strength state come from
its own play-by-play and shift chart, public at 10:00 UTC the morning after (ADR 0004), so no
prediction for the game ever sees them. The column set is locked here, as for the other per-game
tables."""

import pytest
from feed_fixtures import MTL_ARI, OPENING_WEEK_GAMES, assert_public_the_morning_after, parsed_feeds

from nhl_edge.lake.schemas import StrengthTime, dtypes

GAMES = (*OPENING_WEEK_GAMES, MTL_ARI)

COLUMNS = [
    "game_id",
    "season",
    "game_date",
    "team",
    "is_home",
    "strength",
    "own_net_empty",
    "opp_net_empty",
    "strength_source",
    "seconds",
    "game_seconds",
    "observed_utc",
    "raw_key",
]


@pytest.mark.parametrize("game_id", GAMES)
def test_a_game_never_sees_its_own_strength_time(game_id: int) -> None:
    assert_public_the_morning_after(parsed_feeds(game_id)["strength_time"], game_id)


def test_strength_time_holds_only_time_and_its_sources() -> None:
    assert list(dtypes(StrengthTime)) == COLUMNS
