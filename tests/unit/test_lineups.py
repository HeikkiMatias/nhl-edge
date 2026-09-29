import json

import polars as pl
import pytest
from feed_fixtures import MTL_ARI, TRIMMED_GAMES, feed, feed_game, games_row, raw_key, trimmed

from nhl_edge.ingest.feeds import FeedGame, clock_s
from nhl_edge.ingest.lineups import parse_actual_lineups

BURNS = 8470613


def lineup_of(game_id: int) -> pl.DataFrame:
    game = feed_game(game_id)
    return parse_actual_lineups(feed("boxscore", game_id), game, raw_key("boxscore", game))


def test_every_dressed_player_with_his_role() -> None:
    lineup = lineup_of(MTL_ARI)
    assert lineup.height == 40
    per_team = lineup.group_by("team", "is_home", "role").len().sort("team", "role")
    assert per_team.rows() == [
        ("ARI", False, "D", 6),
        ("ARI", False, "F", 12),
        ("ARI", False, "G", 2),
        ("MTL", True, "D", 6),
        ("MTL", True, "F", 12),
        ("MTL", True, "G", 2),
    ]
    assert lineup["raw_key"].unique().to_list() == [
        "nhl/boxscore/20222023/2022020060/20260928T130000Z"
    ]


def test_starting_goalie_and_time_on_ice_from_the_boxscore() -> None:
    lineup = lineup_of(MTL_ARI)
    stats = json.loads(feed("boxscore", MTL_ARI))["playerByGameStats"]
    for side in ("homeTeam", "awayTeam"):
        for goalie in stats[side]["goalies"]:
            row = lineup.filter(pl.col("player_id") == goalie["playerId"]).row(0, named=True)
            assert row["starting_goalie"] is goalie["starter"]
            assert row["toi_s"] == clock_s(goalie["toi"])
    assert lineup.filter("starting_goalie").height == 2
    assert lineup["toi_s"].null_count() == 0


def test_role_is_the_boxscore_group_not_the_position_code() -> None:
    # Burns played forward all 2013-14 season, and the boxscore lists him with forwards, but with
    # the position code D he has today.
    body = trimmed("boxscore", 2013020014)
    burns = next(
        p
        for p in json.loads(body)["playerByGameStats"]["homeTeam"]["forwards"]
        if p["playerId"] == BURNS
    )
    assert burns["position"] == "D"
    lineup = parse_actual_lineups(body, TRIMMED_GAMES[2013020014], "k")
    assert lineup.filter(pl.col("player_id") == BURNS)["role"].to_list() == ["F"]
    assert "position" not in lineup.columns


def test_a_boxscore_of_another_game_or_other_teams_is_rejected() -> None:
    with pytest.raises(ValueError, match="is for game 2010020004"):
        parse_actual_lineups(feed("boxscore", 2010020004), feed_game(2010020003), "k")
    row = games_row(2010020003) | {"home": "CAR", "away": "MIN"}
    with pytest.raises(ValueError, match="CAR at MIN, games has MIN at CAR"):
        FeedGame.from_boxscore(row, feed("boxscore", 2010020003))
