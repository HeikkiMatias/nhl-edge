"""The market input of each historical experiment, from the SBR archive (docs/plan.md section 1,
ADR 0006).

E1, the information test, compares with the de-vigged close as of the start. SBR's close is the
last price before the start and is observed at the start, so E1's prediction time is the start.
No pre-game prediction reads it.

E2, the tradable prediction, bets at the price available at the prediction time: historically the
SBR opener, assumed available from 10:00 US Eastern on the game date. Each game is predicted one
second after its opener's assumed time and reads it through sbr.assumed_available_at, the one
selector for that assumption. A game whose opener is assumed only at its start (capped by
open_assumed_utc) has no E2 price, since the opener is no longer executable then.
"""

from datetime import timedelta
from enum import StrEnum

import polars as pl

from nhl_edge.ingest.sbr import assumed_available_at


class Experiment(StrEnum):
    E1 = "E1"  # information test: the close, at the start
    E2 = "E2"  # tradable prediction: the opener, at its assumed time


# An E2 prediction runs this long after its opener's assumed time: assumed_available_at shows a
# price only strictly after it.
PREDICTION_LAG = timedelta(seconds=1)

PRICE_SCHEMA = {
    "game_id": pl.Int64,
    "season": pl.Int32,
    "game_date": pl.Date,
    "start_utc": pl.Datetime("us", "UTC"),
    "prediction_utc": pl.Datetime("us", "UTC"),
    "home_price": pl.Float64,
    "away_price": pl.Float64,
}


def _pair(quotes: pl.DataFrame) -> pl.DataFrame:
    """One row per game: its home and away decimal prices, from one quote of the moneyline."""
    index = ["game_id", "season", "game_date", "start_utc", "prediction_utc"]

    def side(name: str) -> pl.DataFrame:
        return quotes.filter(pl.col("side") == name).select(
            *index, pl.col("price_decimal").alias(f"{name}_price")
        )

    return (
        side("home")
        .join(side("away").select("game_id", "away_price"), on="game_id")
        .select(list(PRICE_SCHEMA))
        .sort("game_id")
    )


def market_prices(sbr_odds: pl.DataFrame, experiment: Experiment) -> pl.DataFrame:
    """Each game's moneyline prices for the experiment, with the time its prediction runs."""
    h2h = sbr_odds.filter(pl.col("market") == "h2h")
    if experiment is Experiment.E1:
        closes = h2h.filter(pl.col("quote") == "close")
        return _pair(closes.with_columns(prediction_utc=pl.col("start_utc")))
    opens = h2h.filter(pl.col("quote") == "open")
    parts = []
    for (assumed,), group in opens.group_by("assumed_available_utc"):
        prediction_utc = assumed + PREDICTION_LAG  # type: ignore[operator]
        parts.append(
            assumed_available_at(group, prediction_utc).with_columns(
                prediction_utc=pl.lit(prediction_utc, PRICE_SCHEMA["prediction_utc"])
            )
        )
    if not parts:
        return pl.DataFrame(schema=PRICE_SCHEMA)
    return _pair(pl.concat(parts))
