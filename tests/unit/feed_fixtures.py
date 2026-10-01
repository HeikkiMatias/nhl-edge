"""Real per-game feeds copied from the raw cache, shared by the parser, ingest and leakage tests.

Full feeds (gzipped, byte-for-byte what the API returned):
- 2010020003, 2010020004, 2010020008: the 2010-11 opening week, won in regulation, overtime and
  a shootout. Play-by-play before 2019-20 does not say which end each team attacks.
- 2022020060: MTL beat ARI 6-2, with a penalty-shot goal and an empty-net goal from MTL's own zone.

Trimmed edge cases (JSON, fields the parsers do not read removed): see TRIMMED_GAMES.
"""

import gzip
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from nhl_edge.ingest.feeds import FeedGame
from nhl_edge.ingest.games import result_public_utc
from nhl_edge.ingest.nhl_api import parse_utc
from nhl_edge.ingest.nhl_ingest import parse_feeds
from nhl_edge.lake.schemas import MIN_RESULT_LAG
from nhl_edge.lake.tables import known_at

FIXTURES = Path(__file__).parent / "fixtures" / "nhl_api"
FEEDS = FIXTURES / "feeds"
OPENING_WEEK_GAMES = (2010020003, 2010020004, 2010020008)
MTL_ARI = 2022020060

# Games of the trimmed fixtures, with the NHL team ids their feeds use.
TRIMMED_GAMES = {
    # penalties and faceoffs: a bench minor served by a player, a minor without a drawer
    2010020001: FeedGame(2010020001, 20102011, date(2010, 10, 7), "TOR", "MTL", 10, 8),
    # penalties and faceoffs: an overtime penalty, a game misconduct logged in the shootout
    2013021096: FeedGame(2013021096, 20132014, date(2014, 3, 27), "TBL", "NYI", 14, 2),
    # penalties and faceoffs: a head coach's game misconduct naming no player
    2022020993: FeedGame(2022020993, 20222023, date(2023, 3, 4), "SJS", "WSH", 28, 15),
    # 21 unblocked shots without a situationCode
    2010020124: FeedGame(2010020124, 20102011, date(2010, 10, 27), "CAR", "WSH", 12, 15),
    # Brent Burns in the forwards group with position code D
    2013020014: FeedGame(2013020014, 20132014, date(2013, 10, 3), "SJS", "VAN", 28, 23),
    # blank-ended shifts with duration 00:00
    2019020003: FeedGame(2019020003, 20192020, date(2019, 10, 2), "EDM", "VAN", 22, 23),
    # STL and MIN shifts in the WSH at NYI chart
    2021020513: FeedGame(2021020513, 20212022, date(2022, 4, 28), "NYI", "WSH", 2, 15),
    # overtime and shootout rows
    2021020972: FeedGame(2021020972, 20212022, date(2022, 3, 17), "STL", "PIT", 19, 5),
    # shifts listed twice under new row ids
    2022020041: FeedGame(2022020041, 20222023, date(2022, 10, 17), "BOS", "FLA", 6, 13),
}


def feed(kind: str, game_id: int) -> bytes:
    """A full feed: kind is play-by-play, boxscore or shiftcharts."""
    return gzip.decompress((FEEDS / f"{kind}_{game_id}.json.gz").read_bytes())


def trimmed(kind: str, game_id: int) -> bytes:
    return (FIXTURES / f"{kind}_{game_id}_trimmed.json").read_bytes()


def games_row(game_id: int) -> dict[str, Any]:
    """The games-table fields the feed parsers read, from a full fixture's boxscore."""
    box = json.loads(feed("boxscore", game_id))
    return {
        "game_id": game_id,
        "season": box["season"],
        "game_date": date.fromisoformat(box["gameDate"]),
        "home": box["homeTeam"]["abbrev"],
        "away": box["awayTeam"]["abbrev"],
    }


def feed_game(game_id: int) -> FeedGame:
    return FeedGame.from_boxscore(games_row(game_id), feed("boxscore", game_id))


def raw_key(kind: str, game: FeedGame) -> str:
    return f"nhl/{kind}/{game.season}/{game.game_id}/20260928T130000Z"


def parsed_feeds(game_id: int) -> dict[str, pl.DataFrame]:
    """A full fixture game parsed into the per-game tables, as nhl ingest does."""
    game = feed_game(game_id)
    kinds = ("play-by-play", "boxscore", "shiftcharts")
    feeds = {kind: (feed(kind, game_id), raw_key(kind, game)) for kind in kinds}
    return parse_feeds(games_row(game_id), feeds)


def assert_public_the_morning_after(frame: pl.DataFrame, game_id: int) -> None:
    """No row of the game is known at its start, six hours later, or at 10:00 UTC the next
    morning itself; every row is known right after."""
    start = start_utc(game_id)
    public = result_public_utc(feed_game(game_id).game_date)
    rows = frame.filter(pl.col("game_id") == game_id)
    assert rows.height > 0
    for moment in (start, start + MIN_RESULT_LAG, public):
        assert known_at(rows, moment).is_empty(), moment
    assert known_at(rows, public + timedelta(seconds=1)).height == rows.height
    assert (rows["observed_utc"] >= start + MIN_RESULT_LAG).all()


def start_utc(game_id: int) -> datetime:
    return parse_utc(json.loads(feed("play-by-play", game_id))["startTimeUTC"])
