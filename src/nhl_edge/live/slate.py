"""The slate (#162): the regular-season games scheduled for a game date, from the NHL schedule
response for that date, as the targets the live feature rows and predictions rate.

A /v1/schedule/{date} response lists the seven days from that date. The slate keeps the date's
regular-season games whose schedule state is OK, so a postponed or cancelled game is left out.
Each row's observed_utc is the response's actual fetch time (its meta's fetched_utc): the slate is
what was known then, never a backdated convention.
"""

from datetime import date
from typing import Any

import polars as pl

from nhl_edge.ingest.games import LIMITED_ATTENDANCE_SEASONS, listed_games
from nhl_edge.ingest.nhl_api import parse_utc
from nhl_edge.lake.schemas import Slate, dtypes

SCHEDULED = "OK"


def slate_rows(body: bytes, meta: dict[str, Any], day: date, raw_key: str) -> pl.DataFrame:
    """The day's scheduled regular-season games in a schedule response, validated against Slate."""
    observed = parse_utc(meta["fetched_utc"])
    rows = [
        {
            "game_id": game["id"],
            "season": game["season"],
            "game_date": game_date,
            "start_utc": parse_utc(game["startTimeUTC"]),
            "home": game["homeTeam"]["abbrev"],
            "away": game["awayTeam"]["abbrev"],
            "venue": game["venue"]["default"],
            "neutral_site": game["neutralSite"],
            "limited_attendance": game["season"] in LIMITED_ATTENDANCE_SEASONS,
            "game_state": game["gameState"],
            "observed_utc": observed,
            "raw_key": raw_key,
        }
        for game_date, game in listed_games(body, [day])
        if game.get("gameScheduleState") == SCHEDULED
    ]
    frame = pl.DataFrame(rows, schema=dtypes(Slate)).sort("game_id")
    return Slate.validate(frame)


def as_games(slate: pl.DataFrame) -> pl.DataFrame:
    """The slate's games with the schedule columns the feature builders rate a game by."""
    return slate.select("game_id", "season", "game_date", "start_utc", "home", "away")
