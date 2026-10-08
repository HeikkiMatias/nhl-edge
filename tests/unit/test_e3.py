from datetime import UTC, date, datetime
from typing import Any, cast

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
    valued = e3.closing_value(settled, e3.sbr_closes(odds))
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


def test_the_sensitivity_leaves_out_only_the_listed_bets() -> None:
    n = 40
    valued = pl.DataFrame(
        {
            "season": [20212022] * n,
            "game_id": list(range(n)),
            "game_date": [date(2021, 10, 12 + k // 4) for k in range(n)],
            # The listed bets carry an outsized CLV, so leaving them out moves the mean.
            "clv": [0.5 if k in (3, 7) else 0.01 for k in range(n)],
            "fair_move": [0.02] * n,
            "stake": [1.0] * n,
            "ret": [0.1] * n,
        }
    )
    groups = valued.select(
        "game_id", different_favourites=pl.lit(False), early_season=pl.lit(False)
    )
    # A listed game the policy never bet doesn't count, nor does a listed bet without a close,
    # which E3's pooled figures never held.
    unclosed = valued.with_columns(
        clv=pl.when(pl.col("game_id") == 11).then(None).otherwise(pl.col("clv"))
    )
    found = e3.without_suspects(unclosed, pl.Series("game_id", [3, 7, 11, 999]), groups)
    assert found["bets_left_out"] == 2
    assert (
        found["pooled"]
        == e3.report(unclosed.filter(~pl.col("game_id").is_in([3, 7])), groups)["pooled"]
    )
    found = e3.without_suspects(valued, pl.Series("game_id", [3, 7, 999]), groups)
    assert found["bets_left_out"] == 2
    assert found["pooled"]["bets"] == n - 2
    assert found["pooled"]["clv_per_bet"]["mean"] == pytest.approx(0.01)
    kept = valued.filter(~pl.col("game_id").is_in([3, 7]))
    assert found["pooled"] == e3.report(kept, groups)["pooled"]
    # An empty list leaves E3 as it is.
    nothing = e3.without_suspects(valued, pl.Series("game_id", [], dtype=pl.Int64), groups)
    assert nothing["bets_left_out"] == 0
    assert nothing["pooled"] == e3.report(valued, groups)["pooled"]


def test_each_bet_is_attributed_to_the_part_that_pushes_it_most() -> None:
    tables = league(seasons=(20162017, 20172018, 20182019, 20192020))
    fits, history = {}, []
    for season in (20182019, 20192020):
        start = fold_start(tables.games.select("season", "start_utc"), season)
        games = tables.games.filter(pl.col("season") == season).head(30)
        moments = games.select("game_id", prediction_utc="start_utc")
        predicted, fits[season] = b3.predictions(tables, moments, season, start)
        # A market that agrees with B3: each input's usual level follows its share of it.
        history.append(
            predicted.join(moments, on="game_id").select(
                pl.lit(season, pl.Int32).alias("season"),
                "game_id",
                "prediction_utc",
                logit_mkt=(pl.col("p_home") / (1 - pl.col("p_home"))).log(),
            )
        )
    market = pl.concat(history)
    season = 20192020
    bets = (
        market.filter(pl.col("season") == season)
        .select("game_id", "prediction_utc", "season")
        .with_columns(side=pl.lit("home"))
    )
    fit = Blend(Kind.MODEL, (0.0, 0.5, 0.7, 0.1), (0.1,) * 4, 1000, fits[season].train_cutoff)
    inputs = market.select("game_id", "logit_mkt", u=pl.lit(0.0))
    home = e3.attribution(bets, tables, fits, {season: fit}, inputs, market)
    away = e3.attribution(
        bets.with_columns(side=pl.lit("away")), tables, fits, {season: fit}, inputs, market
    )
    assert home.height == 30
    assert set(home["driver"].unique()) <= set(e3.PARTS)
    for part in e3.PARTS:
        assert home[f"part_{part}"].to_list() == pytest.approx((-away[f"part_{part}"]).to_list())
    # The parts add up to the blend's move from the market: a + (b_m - 1)·L + w·logit p_B3.
    parts = e3.b3_parts(bets, tables, fits[season], season).join(inputs, on="game_id")
    moved = parts.select(
        "game_id",
        moved=0.5 * pl.col("logit_mkt")
        - pl.col("logit_mkt")
        + 0.7 * pl.sum_horizontal("intercept", *e3.INPUTS),
    )
    total = home.select("game_id", total=pl.sum_horizontal(*(f"part_{p}" for p in e3.PARTS)))
    both = total.join(moved, on="game_id")
    assert both["total"].to_list() == pytest.approx(both["moved"].to_list())
    # The first season has no earlier one to learn the usual levels from: no driver.
    first = bets.with_columns(season=pl.lit(20182019, pl.Int32))
    found = e3.attribution(first, tables, fits, {20182019: fit}, inputs, market)
    assert found["driver"].is_null().all()


def test_an_edge_from_one_input_is_attributed_to_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # #154: the inputs' usual parts are shares of the market's log-odds, so a bet where only the
    # goalies depart from theirs is driven by the goalies, favourite or underdog alike, and the
    # blend's reshaping of the market is no part of it.
    usual = {"skaters": (0.0, 0.6), "goalies": (0.0, 0.3), "home_ice": (0.1, 0.0)}
    usual["schedule"] = (-0.1, 0.1)
    logits = {1: -1.0, 2: 0.5, 3: 1.2, 11: 0.5, 12: -0.8}
    unusual = {11: 0.4, 12: -0.4}

    def parts(rows: pl.DataFrame, tables: Any, model: Any, season: int) -> pl.DataFrame:
        frame = []
        for game_id in rows["game_id"].to_list():
            level = logits[game_id]
            row = {part: m + s * level for part, (m, s) in usual.items()}
            row["goalies"] += unusual.get(game_id, 0.0)
            frame.append({"game_id": game_id, "intercept": 0.0, **row})
        return pl.DataFrame(frame)

    monkeypatch.setattr(e3, "b3_parts", parts)
    history = pl.DataFrame(
        {
            "season": [20202021] * 3 + [20212022] * 2,
            "game_id": [1, 2, 3, 11, 12],
            "prediction_utc": [datetime(2021, 1, 1, tzinfo=UTC)] * 5,
            "logit_mkt": [logits[g] for g in (1, 2, 3, 11, 12)],
        }
    )
    bets = pl.DataFrame(
        {"season": [20212022, 20212022], "game_id": [11, 12], "side": ["home", "away"]}
    )
    # a = 0 and b_m + b_x·Σs = 1: the blend keeps the market's scale.
    fit = Blend(Kind.MODEL, (0.0, 0.3, 0.7, 0.0), (0.1,) * 4, 1000, datetime(2021, 1, 1))
    inputs = history.select("game_id", "logit_mkt", u=pl.lit(0.0))
    # b3_parts is replaced, so neither B3's tables nor its fits are read.
    tables = cast(b3.Tables, None)
    fits = cast(dict[int, b3.B3Model], {20202021: None, 20212022: None})
    found = e3.attribution(bets, tables, fits, {20212022: fit}, inputs, history)
    assert found["driver"].to_list() == ["goalies", "goalies"]
    for row in found.iter_rows(named=True):
        assert row["part_goalies"] == pytest.approx(0.7 * 0.4)
        for part in ("market", "skaters", "home_ice", "schedule"):
            assert row[f"part_{part}"] == pytest.approx(0.0, abs=1e-12)
