"""The slate (#162): the regular-season games scheduled for a game date, from the NHL schedule
response for that date, as the targets the live feature rows and predictions rate.

A /v1/schedule/{date} response lists the seven days from that date. The slate keeps the date's
regular-season games whose schedule state is OK, so a postponed or cancelled game is left out.
Each row's observed_utc is the response's actual fetch time: the slate is what was known then,
never a backdated convention.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import polars as pl

from nhl_edge.ingest.games import FINAL, LIMITED_ATTENDANCE_SEASONS, listed_games, settled_on
from nhl_edge.ingest.nhl_api import NhlApi, never, parse_utc
from nhl_edge.lake.schemas import Slate, dtypes

SCHEDULED = "OK"
# The days before a slate whose games must all be final in the lake before it is rated: a game
# missing from them would drop out of its teams' histories unnoticed (#170).
SETTLED_DAYS = 7


def slate_rows(body: bytes, fetched_utc: datetime, day: date, raw_key: str) -> pl.DataFrame:
    """The day's scheduled regular-season games in a schedule response, validated against Slate."""
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
            "observed_utc": fetched_utc,
            "raw_key": raw_key,
        }
        for game_date, game in listed_games(body, [day])
        if game.get("gameScheduleState") == SCHEDULED
    ]
    frame = pl.DataFrame(rows, schema=dtypes(Slate)).sort("game_id")
    return Slate.validate(frame)


@dataclass(frozen=True)
class Fetched:
    """A day's slate, and the schedule response it came from, which an empty slate needs too."""

    games: pl.DataFrame
    raw_key: str
    fetched_utc: datetime


def fetch(api: NhlApi, day: date) -> Fetched:
    """The day's slate from a schedule response fetched now, never a cached one: a game can be
    postponed or re-timed up to its start."""
    response = api.schedule_week(day, never)
    games = slate_rows(response.body, response.fetched_utc, day, response.raw_key)
    return Fetched(games, response.raw_key, response.fetched_utc)


def of_day(slate: pl.DataFrame, day: date) -> pl.DataFrame:
    """The slate table's games of one date."""
    return slate.filter(pl.col("game_date") == day).sort("game_id")


def unsettled(body: bytes, days: Collection[date], games: pl.DataFrame) -> list[str]:
    """Why the lake's history is not ready for a slate: a regular-season game scheduled on the
    days before it that is not final, or final but not in games."""
    problems = []
    for game_date, game in listed_games(body, days):
        if game.get("gameScheduleState") != SCHEDULED:
            continue
        if game["gameState"] != FINAL:
            problems.append(f"{game_date}: game {game['id']} is {game['gameState']}, not final")
        elif games.filter(pl.col("game_id") == game["id"]).is_empty():
            problems.append(f"{game_date}: game {game['id']} is final but not in the lake's games")
    return problems


def opening(slate: pl.DataFrame, games: pl.DataFrame) -> list[int]:
    """The slate's seasons with no final game in games yet, whose opening night it is (#181):
    they have no shots to score or stints to build, and the other builders rate the slate from
    the earlier seasons and the season's start alone, as history rates its first night."""
    return sorted(set(slate["season"].to_list()) - set(games["season"].to_list()))


def settled_problems(api: NhlApi, day: date, games: pl.DataFrame) -> list[str]:
    """unsettled() over the SETTLED_DAYS before day, from their schedule response: a cached one
    only if every game on those days was final when it was fetched."""
    first = day - timedelta(days=SETTLED_DAYS)
    days = [first + timedelta(days=k) for k in range(SETTLED_DAYS)]
    response = api.schedule_week(first, settled_on(days))
    return unsettled(response.body, days, games)
