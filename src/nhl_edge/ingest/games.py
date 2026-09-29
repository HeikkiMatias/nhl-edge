"""Games: final regular-season games from the NHL weekly schedule (docs/plan.md section 4).

A /v1/schedule/{date} response lists the seven days from that date. Each game carries its state
(OFF is final and official), both full-game scores, gameOutcome.lastPeriodType (REG, OT or SO), the
venue and neutralSite. Postponed games appear only on their rescheduled date, so a day never lists
a game twice.
"""

import json
from collections.abc import Collection
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import polars as pl

from nhl_edge.ingest.nhl_api import REGULAR_SEASON, Reuse, parse_utc
from nhl_edge.lake.schemas import SCHEDULE_LEAD, Games, Schedule, dtypes

FINAL = "OFF"
# The API has no end-of-game time, and a delayed game can finish many hours after its scheduled
# start (the 2021 Lake Tahoe game ended about 11 hours after it). A result counts as public at
# 10:00 UTC the morning after its game date (ADR 0003): after the nightly ingest, before the
# morning odds slot, and at least six hours after any start.
RESULT_PUBLIC_AT = time(10, 0, tzinfo=UTC)


def result_public_utc(game_date: date) -> datetime:
    return datetime.combine(game_date + timedelta(days=1), RESULT_PUBLIC_AT)


# Played without fans or with capped crowds throughout.
LIMITED_ATTENDANCE_SEASONS = frozenset({20202021})
# Regular-season games per season: the 2012-13 lockout, the 2019-20 pause and the 56-game 2020-21
# season were shortened; VGK joined in 2017-18 and SEA in 2021-22.
EXPECTED_GAMES = {
    20102011: 1230,
    20112012: 1230,
    20122013: 720,
    20132014: 1230,
    20142015: 1230,
    20152016: 1230,
    20162017: 1230,
    20172018: 1271,
    20182019: 1271,
    20192020: 1082,
    20202021: 868,
    20212022: 1312,
    20222023: 1312,
    20232024: 1312,
    20242025: 1312,
    20252026: 1312,
}


def probe_date(season: int) -> date:
    """A date inside every regular season, February 15 of its second year, whose schedule
    response gives the season's bounds."""
    return date(season % 10_000, 2, 15)


def season_bounds(body: bytes) -> tuple[date, date]:
    """First and last day of the regular season that contains the requested date."""
    data = json.loads(body)
    return (
        date.fromisoformat(data["regularSeasonStartDate"]),
        date.fromisoformat(data["regularSeasonEndDate"]),
    )


def season_over(body: bytes, meta: dict[str, Any]) -> bool:
    """Reuse a cached season probe only if it was fetched after the regular season ended: until
    then, postponements can still move the end date."""
    _, end = season_bounds(body)
    return parse_utc(meta["fetched_utc"]).date() > end


def listed_games(body: bytes, days: Collection[date]) -> list[tuple[date, dict[str, Any]]]:
    """Every regular-season game the response lists on the given days, with its day."""
    listed: list[tuple[date, dict[str, Any]]] = []
    for day in json.loads(body)["gameWeek"]:
        game_date = date.fromisoformat(day["date"])
        if game_date in days:
            listed.extend((game_date, g) for g in day["games"] if g["gameType"] == REGULAR_SEASON)
    return listed


def settled_on(days: Collection[date]) -> Reuse:
    """Reuse a cached week only when every regular-season game on the days used is final."""

    def reuse(body: bytes, meta: dict[str, Any]) -> bool:
        return all(game["gameState"] == FINAL for _, game in listed_games(body, days))

    return reuse


def game_row(game_date: date, game: dict[str, Any], raw_key: str) -> dict[str, Any]:
    start_utc = parse_utc(game["startTimeUTC"])
    return {
        "game_id": game["id"],
        "season": game["season"],
        "game_date": game_date,
        "start_utc": start_utc,
        "home": game["homeTeam"]["abbrev"],
        "away": game["awayTeam"]["abbrev"],
        "venue": game["venue"]["default"],
        "home_score": game["homeTeam"]["score"],
        "away_score": game["awayTeam"]["score"],
        "decided_in": game["gameOutcome"]["lastPeriodType"],
        "neutral_site": game["neutralSite"],
        "limited_attendance": game["season"] in LIMITED_ATTENDANCE_SEASONS,
        "observed_utc": result_public_utc(game_date),
        "raw_key": raw_key,
    }


def parse_games(listed: list[tuple[date, dict[str, Any]]], raw_key: str) -> pl.DataFrame:
    """The final games among those listed, validated against Games."""
    rows = [game_row(day, game, raw_key) for day, game in listed if game["gameState"] == FINAL]
    return Games.validate(pl.DataFrame(rows, schema=dtypes(Games)))


def schedule_public_utc(start_utc: datetime) -> datetime:
    """When a game's schedule counts as public: a day before its start (ADR 0005)."""
    return start_utc - SCHEDULE_LEAD


def schedule_of(games: pl.DataFrame) -> pl.DataFrame:
    """The pre-game facts of final games, public a day before each start, validated against
    Schedule. Derived from the same rows as games, so the two never disagree on a game."""
    schedule = games.with_columns(observed_utc=pl.col("start_utc") - SCHEDULE_LEAD)
    return Schedule.validate(schedule.select(list(dtypes(Schedule))))


def results_known_at(games: pl.DataFrame, prediction_utc: datetime) -> pl.DataFrame:
    """Games whose result a prediction at prediction_utc may use: public before that moment."""
    return games.filter(pl.col("observed_utc") < prediction_utc)
