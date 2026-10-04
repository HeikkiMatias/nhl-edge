from datetime import UTC, date, datetime

import numpy as np
import polars as pl
import pytest
from b3_fixtures import league

from nhl_edge.backtest import e3
from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.game import b3
from nhl_edge.market.blend import Blend, Kind

UTC_TYPE = pl.Datetime("us", "UTC")


def sbr(game_id: int, close_home: float, close_away: float) -> pl.DataFrame:
    """One game's SBR moneyline close, as sbr_odds holds it."""
    start = datetime(2021, 11, 10, 23, tzinfo=UTC)
    return pl.DataFrame(
        [
            {
                "game_id": game_id,
                "season": 20212022,
                "game_date": date(2021, 11, 10),
                "start_utc": start,
                "market": "h2h",
                "side": side,
                "quote": "close",
                "price_decimal": price,
                "assumed_available_utc": start,
            }
            for side, price in (("home", close_home), ("away", close_away))
        ],
        schema_overrides={
            "start_utc": UTC_TYPE,
            "assumed_available_utc": UTC_TYPE,
            "season": pl.Int32,
        },
    )


def test_clv_is_the_price_taken_times_the_fair_closing_probability() -> None:
    settled = pl.DataFrame(
        {
            "game_id": [1, 2, 3],
            "side": ["home", "away", "home"],
            "price": [2.10, 2.10, 2.0],
            "home_price": [2.10, 1.80, 2.0],
            "away_price": [1.80, 2.10, 1.85],
        }
    )
    odds = pl.concat([sbr(1, 1.90, 2.00), sbr(2, 1.90, 2.00)])
    valued = e3.closing_value(settled, odds)
    # Game 3 has no close, so no CLV, but it stays in the ledger.
    assert valued["game_id"].to_list() == [1, 2, 3]
    assert valued["clv"][2] is None
    p_home = (1 / 1.90) / (1 / 1.90 + 1 / 2.00)
    assert valued["p_close"].to_list()[:2] == pytest.approx([p_home, 1 - p_home])
    assert valued["clv"].to_list()[:2] == pytest.approx(
        [2.10 * p_home - 1, 2.10 * (1 - p_home) - 1]
    )
    # The fair move leaves both books' margins out: the close's fair probability over the
    # opener's, less 1.
    open_home = (1 / 2.10) / (1 / 2.10 + 1 / 1.80)
    assert valued["fair_move"][0] == pytest.approx(p_home / open_home - 1)


def test_the_report_gives_clv_per_bet_and_by_stake_with_intervals() -> None:
    rng = np.random.default_rng(1)
    n = 60
    valued = pl.DataFrame(
        {
            "season": [20212022] * n,
            "game_id": list(range(n)),
            "game_date": [date(2021, 10, 12 + k // 4) for k in range(n)],
            "clv": rng.normal(0.01, 0.03, n),
            "fair_move": rng.normal(0.02, 0.03, n),
            "stake": rng.uniform(0.5, 1.5, n),
            "ret": rng.normal(0.0, 1.0, n),
            "driver": ["skaters", "market"] * (n // 2),
        }
    )
    groups = valued.select(
        "game_id",
        different_favourites=pl.col("game_id") % 5 == 0,
        early_season=pl.col("game_id") < 10,
    )
    found = e3.report(valued, groups)
    pooled = found["pooled"]
    assert pooled["bets"] == n
    assert (
        pooled["clv_per_bet"]["low"] < pooled["clv_per_bet"]["mean"] < pooled["clv_per_bet"]["high"]
    )
    weighted = float((valued["clv"] * valued["stake"]).sum()) / float(valued["stake"].sum())
    assert pooled["clv_stake_weighted"]["value"] == pytest.approx(weighted)
    assert found["groups"]["different_favourites"]["bets"] == 12
    assert set(found["drivers"]) == {"skaters", "market"}


def test_each_bet_is_attributed_to_the_part_that_pushes_it_most() -> None:
    tables = league()
    season = 20182019
    start = fold_start(tables.games.select("season", "start_utc"), season)
    games = tables.games.filter(pl.col("season") == season).head(30)
    moments = games.select("game_id", prediction_utc="start_utc")
    _, model = b3.predictions(tables, moments, season, start)
    bets = games.select("game_id", prediction_utc="start_utc").with_columns(
        season=pl.lit(season), side=pl.lit("home")
    )
    fit = Blend(Kind.MODEL, (0.0, 0.5, 0.7, 0.1), (0.1,) * 4, 1000, start)
    market = games.select("game_id", logit_mkt=pl.lit(0.2), u=pl.lit(0.0))
    home = e3.attribution(bets, tables, {season: model}, {season: fit}, market)
    away = e3.attribution(
        bets.with_columns(side=pl.lit("away")), tables, {season: model}, {season: fit}, market
    )
    assert home.height == 30
    assert set(home["driver"].unique()) <= set(e3.PARTS)
    # The market part is a + (b_m - 1)·logit p_mkt, here -0.1 toward the home side.
    assert home["part_market"].to_list() == pytest.approx([-0.1] * 30)
    for part in e3.PARTS:
        assert home[f"part_{part}"].to_list() == pytest.approx((-away[f"part_{part}"]).to_list())
