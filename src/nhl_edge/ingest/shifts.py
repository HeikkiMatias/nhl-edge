"""Shifts: one row per player shift from a game's shift chart (docs/plan.md section 4).

The chart lists shifts (type 517) and goal markers (type 505). Shift times are period clocks,
turned into elapsed game seconds here. Some rows carry no play or cannot be trusted, and are
dropped and counted for shift_coverage:
- dropped: zero-length rows, such as the blank-ended placeholders with duration 00:00 in many
  2019-20 charts; shootout rows (period 5); and repeats of a shift already kept, the same player,
  period and times under the same or another shift number (2022020041 lists 14 shifts twice under
  new row ids, and many 2023-24 charts repeat a shift under the next shift numbers)
- foreign: rows of teams not in the game (2021020513, WSH at NYI, lists its own shifts twice and
  STL and MIN shifts besides)
- bad: malformed times or periods, shifts outside their period, and a shift number repeated with
  other times
"""

import json
from dataclasses import dataclass
from typing import Any

import polars as pl

from nhl_edge.ingest.feeds import FeedGame, clock_s
from nhl_edge.lake.schemas import OT_PERIOD, OT_S, PERIOD_S, Shifts, dtypes

SHIFT = 517


@dataclass(frozen=True)
class ShiftDrops:
    """Shift chart rows left out of Shifts, by reason."""

    dropped: int = 0
    foreign: int = 0
    bad: int = 0


def shift_rows(
    rows: list[dict[str, Any]], game: FeedGame, raw_key: str
) -> tuple[list[dict[str, Any]], ShiftDrops]:
    kept: list[dict[str, Any]] = []
    numbers: set[tuple[int, int, int]] = set()
    times: set[tuple[int, int, int, int]] = set()
    dropped = foreign = bad = 0
    for row in rows:
        if row.get("typeCode") != SHIFT:
            continue
        team = game.team(row.get("teamId", 0))
        if team is None or row.get("gameId") != game.game_id:
            foreign += 1
            continue
        period = row.get("period") or 0
        if period > OT_PERIOD:
            dropped += 1
            continue
        if period < 1:
            bad += 1
            continue
        start, end = clock_s(row.get("startTime")), clock_s(row.get("endTime"))
        if start is not None and (
            end == start or (end is None and clock_s(row.get("duration")) == 0)
        ):
            dropped += 1
            continue
        length = OT_S if period == OT_PERIOD else PERIOD_S
        player, number = row.get("playerId") or 0, row.get("shiftNumber") or 0
        if start is not None and end is not None and (player, period, start, end) in times:
            dropped += 1
            continue
        if (
            start is None
            or end is None
            or not 0 <= start < end <= length
            or not player
            or not number
            or (player, period, number) in numbers
        ):
            bad += 1
            continue
        numbers.add((player, period, number))
        times.add((player, period, start, end))
        offset = (period - 1) * PERIOD_S
        kept.append(
            {
                **game.keys(),
                "team": team,
                "player_id": player,
                "period": period,
                "shift_number": number,
                "start_s": offset + start,
                "end_s": offset + end,
                "observed_utc": game.observed_utc,
                "raw_key": raw_key,
            }
        )
    return kept, ShiftDrops(dropped=dropped, foreign=foreign, bad=bad)


def parse_shifts(body: bytes, game: FeedGame, raw_key: str) -> tuple[pl.DataFrame, ShiftDrops]:
    """The game's shifts, validated against Shifts, and the rows left out."""
    rows, drops = shift_rows(json.loads(body).get("data") or [], game, raw_key)
    return Shifts.validate(pl.DataFrame(rows, schema=dtypes(Shifts))), drops
