"""Odds API quotes for the market move guard's tests (#142)."""

from datetime import UTC, datetime, timedelta

import polars as pl

UTC_TYPE = pl.Datetime("us", "UTC")
DECISION = datetime(2026, 10, 1, 16, 50, tzinfo=UTC)
START = datetime(2026, 10, 1, 23, 0, tzinfo=UTC)


def quotes(
    event: str, slot: str, at: datetime, home: float, away: float, book: str = "pinnacle"
) -> list[dict[str, object]]:
    return [
        {
            "snapshot_utc": at,
            "last_update_utc": at - timedelta(minutes=2),
            "event_id": event,
            "commence_time_utc": START,
            "book": book,
            "market": "h2h",
            "side": side,
            "price_decimal": price,
            "slot": slot,
        }
        for side, price in (("home", home), ("away", away))
    ]


def snapshots(*rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(
        [r for group in rows for r in group],
        schema_overrides={
            "snapshot_utc": UTC_TYPE,
            "last_update_utc": UTC_TYPE,
            "commence_time_utc": UTC_TYPE,
        },
    )
