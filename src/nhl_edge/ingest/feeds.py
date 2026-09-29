"""What the per-game feed parsers share: the game a feed belongs to, and clock parsing.

Play-by-play, boxscore and shift chart are parsed into shots, actual_lineups and shifts, and the
three together into shift_coverage (docs/data-sources.md, "Per-game tables"). Every row of those
tables counts as public at 10:00 UTC the morning after its game date, the rule ADR 0003 sets for
results: the live pipeline fetches the feeds with the results, and a game's own feeds must never
predict it (hard rules 1 and 9).
"""

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from nhl_edge.ingest.games import result_public_utc
from nhl_edge.ingest.nhl_api import clock_s

__all__ = ["FeedGame", "check_game_id", "clock_s"]


@dataclass(frozen=True)
class FeedGame:
    """A final game, with the NHL team ids its feeds use."""

    game_id: int
    season: int
    game_date: date
    home: str
    away: str
    home_id: int
    away_id: int

    @classmethod
    def from_boxscore(cls, game: dict[str, Any], boxscore: bytes) -> "FeedGame":
        """The game of a games row, with team ids from its boxscore. Fails when the boxscore is
        another game's or names other teams."""
        data = json.loads(boxscore)
        check_game_id(data, game["game_id"], "boxscore")
        home, away = data["homeTeam"], data["awayTeam"]
        if (home["abbrev"], away["abbrev"]) != (game["home"], game["away"]):
            raise ValueError(
                f"boxscore of {game['game_id']} has {away['abbrev']} at {home['abbrev']}, "
                f"games has {game['away']} at {game['home']}"
            )
        return cls(
            game_id=game["game_id"],
            season=game["season"],
            game_date=game["game_date"],
            home=game["home"],
            away=game["away"],
            home_id=home["id"],
            away_id=away["id"],
        )

    @property
    def observed_utc(self) -> datetime:
        return result_public_utc(self.game_date)

    def team(self, team_id: int) -> str | None:
        """The triCode of one of the game's two teams, or None for any other team id."""
        return {self.home_id: self.home, self.away_id: self.away}.get(team_id)

    def keys(self) -> dict[str, Any]:
        """The columns every per-game table starts with."""
        return {"game_id": self.game_id, "season": self.season, "game_date": self.game_date}


def check_game_id(data: dict[str, Any], game_id: int, feed: str) -> None:
    if data.get("id") != game_id:
        raise ValueError(f"{feed} for {game_id} is for game {data.get('id')}")
