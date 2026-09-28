"""One pandera schema per table. Every write validates against its schema."""

from datetime import timedelta
from typing import Annotated

import pandera.polars as pa
import polars as pl

UtcDatetime = Annotated[pl.Datetime, "us", "UTC"]


def dtypes(model: type[pa.DataFrameModel]) -> dict[str, pl.DataType]:
    """Column name to Polars dtype, in schema order, for building frames that match a model."""
    return {name: column.dtype.type for name, column in model.to_schema().columns.items()}


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


DECIDED_IN = ("REG", "OT", "SO")
# No result counts as public sooner after its scheduled start (ADR 0003).
MIN_RESULT_LAG = timedelta(hours=6)
TRI_CODE = r"^[A-Z]{3}$"


class Games(pa.DataFrameModel):
    """One final regular-season game (NHL gameState OFF).

    Scores are full-game: a shootout adds one goal for its winner, so home_score > away_score
    settles the moneyline, OT and shootout included. decided_in is the period type that ended the
    game. limited_attendance marks the 2020-21 season, played without fans or with capped crowds.

    observed_utc is when the result (home_score, away_score, decided_in) counts as public:
    10:00 UTC the morning after game_date, a conservative bound because the API has no end time
    (ADR 0003). The schema also requires it to be at least six hours after start_utc.
    The schedule columns (teams, start, venue, neutral_site) were public long before the game,
    but share the row's observed_utc until schedule and results are split (#24).
    """

    game_id: pl.Int64
    season: pl.Int32
    game_date: pl.Date
    start_utc: UtcDatetime
    home: pl.String = pa.Field(str_matches=TRI_CODE)
    away: pl.String = pa.Field(str_matches=TRI_CODE)
    venue: pl.String
    home_score: pl.Int16 = pa.Field(ge=0)
    away_score: pl.Int16 = pa.Field(ge=0)
    decided_in: pl.String = pa.Field(isin=DECIDED_IN)
    neutral_site: pl.Boolean
    limited_attendance: pl.Boolean
    observed_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = "game_id"

    @pa.dataframe_check
    def regular_season_id_of_its_season(cls, data: pa.PolarsData) -> pl.LazyFrame:
        # 2023020001: season start year, game type 02 (regular season), game number
        game_id = pl.col("game_id")
        return data.lazyframe.select(
            (game_id // 1_000_000 == pl.col("season") // 10_000)
            & ((game_id // 10_000) % 100 == 2)
            & (pl.col("season") % 10_000 == pl.col("season") // 10_000 + 1)
        )

    @pa.dataframe_check
    def home_is_not_away(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("home") != pl.col("away"))

    @pa.dataframe_check
    def no_ties(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("home_score") != pl.col("away_score"))

    @pa.dataframe_check
    def extra_time_wins_by_one(cls, data: pa.PolarsData) -> pl.LazyFrame:
        margin = (pl.col("home_score").cast(pl.Int32) - pl.col("away_score")).abs()
        return data.lazyframe.select((pl.col("decided_in") == "REG") | (margin == 1))

    @pa.dataframe_check
    def observed_six_hours_after_start(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(pl.col("observed_utc") >= pl.col("start_utc") + MIN_RESULT_LAG)


class Players(pa.DataFrameModel):
    """One NHL player, from the player landing page.

    Only facts fixed before a player's NHL debut belong here, which is why the table has no
    observed_utc: fetched_utc records provenance. Anything that changes over a career (position,
    team, stats, injuries) goes in a table with observed_utc; the landing page's position is the
    one listed today, so a player's position comes from each game's boxscore instead.
    """

    player_id: pl.Int64
    name: pl.String = pa.Field(str_length={"min_value": 1})
    birth_date: pl.Date
    shoots: pl.String = pa.Field(isin=("L", "R"), nullable=True)
    draft_year: pl.Int16 = pa.Field(nullable=True)
    draft_overall: pl.Int16 = pa.Field(ge=1, nullable=True)
    fetched_utc: UtcDatetime
    raw_key: pl.String

    class Config(pa.DataFrameModel.Config):
        strict = True
        ordered = True
        unique: str | list[str] | None = "player_id"

    @pa.dataframe_check
    def drafted_or_not(cls, data: pa.PolarsData) -> pl.LazyFrame:
        return data.lazyframe.select(
            pl.col("draft_year").is_null() == pl.col("draft_overall").is_null()
        )
