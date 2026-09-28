"""One pandera schema per table. Every write validates against its schema."""

from datetime import timedelta
from typing import Annotated

import pandera.polars as pa
import polars as pl

UtcDatetime = Annotated[pl.Datetime, "us", "UTC"]

ODDS_SIDES = {
    "h2h": ("home", "away"),
    "h2h_3_way": ("home", "draw", "away"),
    "spreads": ("home", "away"),
    "totals": ("over", "under"),
}
ODDS_MARKETS = tuple(ODDS_SIDES)
ODDS_KEY = ("snapshot_utc", "event_id", "book", "market", "side")
# The book's last_update comes from the Odds API clock, snapshot_utc from ours.
CLOCK_SKEW = timedelta(seconds=60)


class OddsSnapshots(pa.DataFrameModel):
    """One quote: snapshot, Odds API event, book, market and side.

    snapshot_utc is when the quote was observed, so it is the time point-in-time filters use.
    last_update_utc is when the book last changed the market, to spot stale quotes. h2h is the
    two-way moneyline, settled on the full game including OT and the shootout. h2h_3_way is the
    regulation line (home, draw, away over 60 minutes) and must never be compared with h2h.
    """

    snapshot_utc: UtcDatetime
    last_update_utc: UtcDatetime
    event_id: pl.String = pa.Field(str_length={"min_value": 1})
    commence_time_utc: UtcDatetime
    home: pl.String = pa.Field(str_matches=r"^[A-Z]{3}$")
    away: pl.String = pa.Field(str_matches=r"^[A-Z]{3}$")
    book: pl.String = pa.Field(str_length={"min_value": 1})
    market: pl.String = pa.Field(isin=ODDS_MARKETS)
    side: pl.String = pa.Field(isin=sorted({s for sides in ODDS_SIDES.values() for s in sides}))
    line: pl.Float64 = pa.Field(nullable=True)
    price_decimal: pl.Float64 = pa.Field(gt=1)
    is_closing_proxy: pl.Boolean
    slot: pl.String
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = list(ODDS_KEY)  # noqa: RUF012 (pandera config)

    @pa.dataframe_check
    def line_null_only_for_h2h_markets(cls, data: pa.PolarsData) -> pl.LazyFrame:
        no_line = pl.col("market").is_in(["h2h", "h2h_3_way"])
        return data.lazyframe.select(no_line == pl.col("line").is_null())

    @pa.dataframe_check
    def side_fits_market(cls, data: pa.PolarsData) -> pl.LazyFrame:
        fits = pl.lit(False)
        for market, sides in ODDS_SIDES.items():
            fits = fits | ((pl.col("market") == market) & pl.col("side").is_in(sides))
        return data.lazyframe.select(fits)

    @pa.dataframe_check
    def last_update_not_after_snapshot(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            pl.col("last_update_utc") <= pl.col("snapshot_utc") + CLOCK_SKEW
        )

    @pa.dataframe_check
    def home_is_not_away(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("home") != pl.col("away"))
