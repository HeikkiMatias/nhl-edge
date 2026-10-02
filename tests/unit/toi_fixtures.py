"""Game 2024021291's time-on-ice reports (#68), shared by the parser, ingest and leakage tests.

The game is one of the 57 of 2024-25 whose shift chart is empty, and it went to overtime. The
fixtures are real pages and the real boxscore, trimmed:
- toi-home_ and toi-visitor_2024021291_trimmed.htm: three players a team (each team's goalie, a
  skater with overtime shifts, and one more: Palat played the first period only). The header's
  Visitor and Home tables, which hold the score, and the footer are left out, and the goal and
  penalty marks are blanked; the parser reads none of them.
- boxscore_2024021291_trimmed.json: the fields the parsers read, for those six players and each
  team's backup goalie, who did not play.

2024-25 is a held-out season, so the play-by-play is not copied: the ingest tests use a synthetic
one with no shots, which runs every parser without revealing anything about the game.
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from nhl_edge.ingest.feeds import FeedGame
from nhl_edge.ingest.lineups import parse_actual_lineups

FIXTURES = Path(__file__).parent / "fixtures" / "nhl_api"
TOI_GAME = 2024021291
TOI_SEASON = 20242025
STAMP = "20261002T120000Z"
# The two teams' shift charts give no shifts: the API's answer for these games.
EMPTY_CHART = json.dumps({"data": [], "total": 0}).encode()
# Sweater numbers of the players kept in the trimmed reports.
SWAYMAN, GEEKIE, LAUKO = 1, 39, 94  # BOS, home
PALAT, ALLEN, HAULA = 18, 34, 56  # NJD, visitors


def toi_page(side: str) -> bytes:
    """A trimmed report: side is home or visitor."""
    return (FIXTURES / f"toi-{side}_{TOI_GAME}_trimmed.htm").read_bytes()


def toi_key(kind: str, game_id: int = TOI_GAME, season: int = TOI_SEASON) -> str:
    """The raw key a response of this kind would have, fetched at STAMP."""
    return f"nhl/{kind}/{season}/{game_id}/{STAMP}"


def toi_reports() -> dict[str, tuple[bytes, str]]:
    """Both trimmed reports, each (body, raw_key) by side, as the parser takes them."""
    return {side: (toi_page(side), toi_key(f"toi-{side}")) for side in ("home", "visitor")}


def toi_boxscore() -> bytes:
    return (FIXTURES / f"boxscore_{TOI_GAME}_trimmed.json").read_bytes()


def toi_games_row() -> dict[str, Any]:
    """The games-table fields the feed parsers read."""
    box = json.loads(toi_boxscore())
    return {
        "game_id": TOI_GAME,
        "season": box["season"],
        "game_date": date.fromisoformat(box["gameDate"]),
        "home": box["homeTeam"]["abbrev"],
        "away": box["awayTeam"]["abbrev"],
    }


def toi_game() -> FeedGame:
    return FeedGame.from_boxscore(toi_games_row(), toi_boxscore())


def toi_lineups() -> pl.DataFrame:
    return parse_actual_lineups(toi_boxscore(), toi_game(), toi_key("boxscore"))


def synthetic_play_by_play(game_id: int = TOI_GAME) -> bytes:
    """A play-by-play with no shots: 5 on 5 from the start to the end of the first period."""

    def play(event_id: int, clock: str, kind: str) -> dict[str, Any]:
        return {
            "eventId": event_id,
            "sortOrder": event_id,
            "periodDescriptor": {"number": 1, "periodType": "REG"},
            "timeInPeriod": clock,
            "typeDescKey": kind,
            "situationCode": "1551",
        }

    plays = [play(1, "00:00", "period-start"), play(2, "20:00", "period-end")]
    return json.dumps({"id": game_id, "plays": plays}).encode()


def toi_feeds() -> dict[str, tuple[bytes, str]]:
    """The game's three feeds as the ingest passes them to parse_feeds, its chart empty."""
    return {
        "play-by-play": (synthetic_play_by_play(), toi_key("play-by-play")),
        "boxscore": (toi_boxscore(), toi_key("boxscore")),
        "shiftcharts": (EMPTY_CHART, toi_key("shiftcharts")),
    }


def player_id(lineups: pl.DataFrame, team: str, sweater_number: int) -> int:
    row = lineups.filter(pl.col("team") == team, pl.col("sweater_number") == sweater_number)
    return row["player_id"].item()
