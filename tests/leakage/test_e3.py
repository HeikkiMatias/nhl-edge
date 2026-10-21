"""E3 (#143) reads the close only to value bets already placed: it never moves a pick, a stake
or a result. The attribution reads B3's inputs at each bet's prediction time, never its result."""

from datetime import UTC, date, datetime

import polars as pl
from b3_fixtures import league
from polars.testing import assert_frame_equal

from nhl_edge.backtest import e3
from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.game import b3
from nhl_edge.market.blend import Blend, Kind

UTC_TYPE = pl.Datetime("us", "UTC")


def test_valuing_against_the_close_never_moves_a_bet() -> None:
    start = datetime(2021, 11, 10, 23, tzinfo=UTC)
    settled = pl.DataFrame(
        {
            "game_id": [1],
            "side": ["home"],
            "price": [2.1],
            "home_price": [2.1],
            "away_price": [1.8],
            "stake": [1.0],
            "profit": [1.1],
            "fraction": [0.01],
        }
    )
    odds = pl.DataFrame(
        [
            {
                "game_id": 1,
                "season": 20212022,
                "game_date": date(2021, 11, 10),
                "start_utc": start,
                "market": "h2h",
                "side": side,
                "quote": "close",
                "price_decimal": price,
                "assumed_available_utc": start,
            }
            for side, price in (("home", 1.5), ("away", 2.8))
        ],
        schema_overrides={
            "start_utc": UTC_TYPE,
            "assumed_available_utc": UTC_TYPE,
            "season": pl.Int32,
        },
    )
    without = settled.with_columns(game_id=pl.lit(2, dtype=pl.Int64))
    both = pl.concat([settled, without])
    valued = e3.closing_value(both, e3.sbr_closes(odds))
    # Every bet stays, the one without a close with no CLV.
    assert_frame_equal(valued.select(both.columns), both)
    assert valued["clv"].is_null().to_list() == [False, True]


def _attributed(tables: b3.Tables) -> tuple[pl.DataFrame, ...]:
    """Bets on 2019-20 with the fits and market rows of 2018-19 and 2019-20."""
    fits, history = {}, []
    for season in (20182019, 20192020):
        start = fold_start(tables.games.select("season", "start_utc"), season)
        games = tables.games.filter(pl.col("season") == season).head(20)
        moments = games.select("game_id", prediction_utc="start_utc")
        _, fits[season] = b3.predictions(tables, moments, season, start)
        history.append(
            moments.with_columns(
                season=pl.lit(season, pl.Int32),
                logit_mkt=(pl.col("game_id") % 7 - 3) / 5,
            )
        )
    market = pl.concat(history)
    bets = market.filter(pl.col("season") == 20192020).select(
        "game_id", "prediction_utc", "season", side=pl.lit("away")
    )
    return bets, market, fits  # type: ignore[return-value]


def test_the_attribution_never_reads_the_result() -> None:
    tables = league(seasons=(20162017, 20172018, 20182019, 20192020))
    bets, market, fits = _attributed(tables)
    fit = Blend(Kind.MODEL, (0.0, 0.5, 0.7, 0.1), (0.1,) * 4, 1000, datetime(2019, 10, 1))
    inputs = market.select("game_id", "logit_mkt", u=pl.lit(0.5))
    flipped = tables.games.with_columns(
        home_score=pl.col("away_score"), away_score=pl.col("home_score")
    )
    swapped = b3.Tables(**{**tables.__dict__, "games": flipped})
    left = e3.attribution(bets, tables, fits, {20192020: fit}, inputs, market)  # type: ignore[arg-type]
    right = e3.attribution(bets, swapped, fits, {20192020: fit}, inputs, market)  # type: ignore[arg-type]
    parts = [f"part_{p}" for p in e3.PARTS]
    assert_frame_equal(
        left.select("game_id", *parts, "driver"), right.select("game_id", *parts, "driver")
    )
    # Each input's usual level is learnt from earlier seasons only (#154): the bet season's own
    # market rows, or a later season's, never move it.
    later = market.with_columns(
        logit_mkt=pl.when(pl.col("season") >= 20192020)
        .then(pl.col("logit_mkt") * 3 + 1)
        .otherwise("logit_mkt")
    )
    moved = e3.attribution(bets, tables, fits, {20192020: fit}, inputs, later)  # type: ignore[arg-type]
    assert_frame_equal(left.select("game_id", *parts), moved.select("game_id", *parts))
