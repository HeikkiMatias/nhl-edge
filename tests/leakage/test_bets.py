"""The policy's bets (#141, ADR 0028) read only what was known when they were placed: E2's opener,
never the close; the blend's probability and u, never the game's result; and a day's stakes read
only the bankroll left by earlier days."""

from datetime import date, datetime, timedelta

import market_history
import polars as pl
from polars.testing import assert_frame_equal

from nhl_edge.backtest import bets
from nhl_edge.betting import selection, staking
from nhl_edge.game import uncertainty

SEASON = 20212022


def setup() -> tuple[
    pl.DataFrame, pl.DataFrame, pl.DataFrame, dict[str, dict[int, uncertainty.Scale]]
]:
    odds, games = market_history.seasons([SEASON], games=24)
    results = games.select(
        "game_id",
        "season",
        "game_date",
        "start_utc",
        home_win=(pl.col("home_score") > pl.col("away_score")).cast(pl.Int8),
    )
    predictions = results.select(
        "season",
        "game_id",
        "game_date",
        "home_win",
        prediction_utc="start_utc",
        experiment=pl.lit("E2"),
        model=pl.lit("BLEND"),
        p_home=0.45 + (pl.col("game_id") % 7) / 50,
    )
    every = results.select(
        "game_id",
        season=pl.col("season").cast(pl.Int32),
        experiment=pl.lit("E2"),
        goalie_doubt=0.2 + (pl.col("game_id") % 5) / 20,
        availability_doubt=pl.lit(1.5),
        rookie_share=pl.lit(0.2),
    )
    first = results["start_utc"].min()
    assert isinstance(first, datetime)
    training = every.with_columns(
        observed_utc=pl.lit(first - timedelta(days=200)),
        train_cutoff=pl.lit(first - timedelta(days=300)),
    )
    scales = {"E2": {SEASON: uncertainty.fit_scale(training)}}
    return odds, predictions, every, scales


def test_the_price_taken_is_the_opener_never_the_close() -> None:
    odds, predictions, every, scales = setup()
    closes = pl.col("quote") == "close"
    moved = odds.with_columns(
        price_decimal=pl.when(closes)
        .then(pl.col("price_decimal") * 1.3)
        .otherwise(pl.col("price_decimal"))
    )
    before = bets.candidates(predictions, every, scales, odds)
    assert before.height == 24
    assert_frame_equal(before, bets.candidates(predictions, every, scales, moved))


def test_the_picks_never_read_the_result() -> None:
    odds, predictions, every, scales = setup()
    flipped = predictions.with_columns(home_win=1 - pl.col("home_win"))
    picked = selection.select(bets.candidates(predictions, every, scales, odds))
    picked_flipped = selection.select(bets.candidates(flipped, every, scales, odds))
    assert_frame_equal(picked.drop("home_win"), picked_flipped.drop("home_win"))


def test_a_days_stakes_never_read_that_day_or_later_days_results() -> None:
    days = [date(2021, 11, 1) + timedelta(days=k // 2) for k in range(6)]
    ledger_in = pl.DataFrame(
        {
            "season": [SEASON] * 6,
            "game_id": list(range(1, 7)),
            "game_date": days,
            "side": ["home"] * 6,
            "price": [2.0] * 6,
            "ev": [0.04] * 6,
            "u_sd": [0.0] * 6,
            "home_win": [1, 0, 1, 1, 0, 0],
        }
    )
    base = staking.settle(ledger_in)
    # Flip the second day's results: its stakes and the first day's stay, the third day's move.
    flipped = staking.settle(
        ledger_in.with_columns(
            home_win=pl.when(pl.col("game_date") == days[2])
            .then(1 - pl.col("home_win"))
            .otherwise(pl.col("home_win"))
        )
    )
    assert base["stake"].to_list()[:4] == flipped["stake"].to_list()[:4]
    assert base["stake"].to_list()[4:] != flipped["stake"].to_list()[4:]
