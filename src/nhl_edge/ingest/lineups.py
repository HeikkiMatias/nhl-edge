"""Actual lineups: the players dressed for a game, from its boxscore (docs/plan.md section 4).

These are the only source for backtest lineups (hard rule 9): a projection may use the boxscores
of earlier games, never the game's own and never a cached roster, which is an after-the-fact
view of a season. The role is the boxscore group a player is listed in, because the position code
next to it is the one listed today.
"""

import json

import polars as pl

from nhl_edge.ingest.feeds import FeedGame, check_game_id, clock_s
from nhl_edge.lake.schemas import ActualLineups, dtypes

ROLE_OF_GROUP = {"forwards": "F", "defense": "D", "goalies": "G"}


def parse_actual_lineups(body: bytes, game: FeedGame, raw_key: str) -> pl.DataFrame:
    """Every player dressed for either team, validated against ActualLineups."""
    data = json.loads(body)
    check_game_id(data, game.game_id, "boxscore")
    stats = data["playerByGameStats"]
    rows = [
        {
            **game.keys(),
            "team": team,
            "is_home": is_home,
            "player_id": player["playerId"],
            "role": role,
            "sweater_number": player.get("sweaterNumber"),
            "starting_goalie": role == "G" and bool(player.get("starter")),
            "toi_s": clock_s(player.get("toi")),
            "observed_utc": game.observed_utc,
            "raw_key": raw_key,
        }
        for side, team, is_home in (("homeTeam", game.home, True), ("awayTeam", game.away, False))
        for group, role in ROLE_OF_GROUP.items()
        for player in stats[side].get(group, [])
    ]
    return ActualLineups.validate(pl.DataFrame(rows, schema=dtypes(ActualLineups)))
