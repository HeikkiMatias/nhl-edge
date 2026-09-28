"""Players: bio and draft facts from the player landing page, for every player on a fetched
roster or in a fetched boxscore (docs/plan.md section 4)."""

import json
from datetime import date, datetime
from typing import Any

import polars as pl

from nhl_edge.lake.schemas import Players, dtypes

ROSTER_GROUPS = ("forwards", "defensemen", "goalies")
BOXSCORE_GROUPS = ("forwards", "defense", "goalies")


def roster_player_ids(body: bytes) -> set[int]:
    """Players on a /v1/roster/{team}/{season} response. Past seasons list everyone who played
    for the team that season."""
    data = json.loads(body)
    return {player["id"] for group in ROSTER_GROUPS for player in data.get(group, [])}


def boxscore_player_ids(body: bytes) -> set[int]:
    """Players dressed for either team in a boxscore."""
    stats = json.loads(body)["playerByGameStats"]
    return {
        player["playerId"]
        for side in ("awayTeam", "homeTeam")
        for group in BOXSCORE_GROUPS
        for player in stats[side].get(group, [])
    }


def landing_row(body: bytes, fetched_utc: datetime, raw_key: str) -> dict[str, Any]:
    data = json.loads(body)
    draft = data.get("draftDetails") or {}
    return {
        "player_id": data["playerId"],
        "name": f"{data['firstName']['default']} {data['lastName']['default']}",
        "birth_date": date.fromisoformat(data["birthDate"]),
        "position": data["position"],
        "shoots": data.get("shootsCatches") or None,
        "draft_year": draft.get("year"),
        "draft_overall": draft.get("overallPick"),
        "fetched_utc": fetched_utc,
        "raw_key": raw_key,
    }


def parse_players(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return Players.validate(pl.DataFrame(rows, schema=dtypes(Players)))
