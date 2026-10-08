"""Synthetic h2h quotes for the closing-proxy tests (#21): one game at 19:00 ET on 2026-10-07,
priced in pairs at the midday decision snapshot and before the start."""

from datetime import UTC, datetime, timedelta

import polars as pl

from nhl_edge.ingest.odds import ODDS_FRAME_SCHEMA

# A 19:00 ET game on 2026-10-07 (EDT): 23:00 UTC.
START = datetime(2026, 10, 7, 23, tzinfo=UTC)
MIDDAY = datetime(2026, 10, 7, 16, 45, 30, tzinfo=UTC)  # 12:45 ET, the decision snapshot
PRE7 = datetime(2026, 10, 7, 22, 45, 30, tzinfo=UTC)  # 18:45 ET
NOW = datetime(2026, 10, 8, 6, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def pair(
    snapshot: datetime,
    start: datetime = START,
    book: str = "pinnacle",
    age: timedelta = MINUTE,
    event: str = "e1",
    sides: tuple[str, ...] = ("home", "away"),
    market: str = "h2h",
) -> list[dict[str, object]]:
    return [
        {
            "snapshot_utc": snapshot,
            "last_update_utc": snapshot - age,
            "event_id": event,
            "commence_time_utc": start,
            "home": "WSH",
            "away": "PIT",
            "book": book,
            "market": market,
            "side": side,
            "line": None,
            "price_decimal": 1.9,
            "is_closing_proxy": False,
            "slot": "x",
            "raw_key": f"odds/{snapshot:%Y%m%dT%H%M%S}",
        }
        for side in sides
    ]


def quotes(*pairs: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame([row for p in pairs for row in p], schema=ODDS_FRAME_SCHEMA)
