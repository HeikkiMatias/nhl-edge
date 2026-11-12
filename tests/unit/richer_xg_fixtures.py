"""Helpers for scripts/richer_xg.py's tests (#421): the script as a module, SYNTHETIC play-by-play
bodies, and shots and players as the lake holds them."""

import importlib.util
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import polars as pl

from nhl_edge.lake.schemas import Players, dtypes

GAME_ID = 2013020001
HOME, AWAY = 1, 2


def load() -> ModuleType:
    """scripts/richer_xg.py as a module."""
    path = Path(__file__).parents[2] / "scripts" / "richer_xg.py"
    spec = importlib.util.spec_from_file_location("richer_xg", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def play(
    event_id: int,
    kind: str,
    clock: str,
    owner: int | None = None,
    x: int | None = None,
    y: int | None = None,
    zone: str | None = None,
    period: int = 1,
    sort_order: int | None = None,
) -> dict[str, Any]:
    """A SYNTHETIC play as the play-by-play feed logs it."""
    details: dict[str, Any] = {}
    if owner is not None:
        details["eventOwnerTeamId"] = owner
    if x is not None:
        details["xCoord"], details["yCoord"] = x, y
    if zone is not None:
        details["zoneCode"] = zone
    return {
        "eventId": event_id,
        "sortOrder": event_id * 10 if sort_order is None else sort_order,
        "typeDescKey": kind,
        "timeInPeriod": clock,
        "periodDescriptor": {"number": period, "periodType": "REG"},
        "details": details,
    }


def body(plays: Sequence[dict[str, Any]], game_id: int = GAME_ID) -> bytes:
    """A SYNTHETIC play-by-play response."""
    return json.dumps({"id": game_id, "plays": list(plays)}).encode()


def players(hands: dict[int, str | None]) -> pl.DataFrame:
    """SYNTHETIC players with their shooting hand."""
    return pl.DataFrame(
        {
            "player_id": list(hands),
            "name": [f"P{p}" for p in hands],
            "birth_date": [date(1990, 1, 1)] * len(hands),
            "shoots": list(hands.values()),
            "draft_year": [None] * len(hands),
            "draft_overall": [None] * len(hands),
            "fetched_utc": [datetime(2026, 1, 1, tzinfo=UTC)] * len(hands),
            "raw_key": ["k"] * len(hands),
        },
        schema=dtypes(Players),
    )


def no_previous(shots: pl.DataFrame) -> pl.DataFrame:
    """Every shot without a previous play, as previous_frame gives it."""
    return shots.select(
        "game_id",
        "event_id",
        prev_event_id=pl.lit(None, dtype=pl.Int64),
        prev_x=pl.lit(None, dtype=pl.Float64),
        prev_y=pl.lit(None, dtype=pl.Float64),
    )
