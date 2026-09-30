from datetime import UTC, datetime

import market_history
import polars as pl
import pytest

from nhl_edge.backtest import sensitivity
from nhl_edge.backtest.market import Experiment, market_prices
from nhl_edge.backtest.walk_forward import bounds, implausible, run

SEASONS = [20172018, 20182019]
TRAINING = 2017020001
TESTED = 2018020002


def test_the_bounds_are_the_range_of_every_earlier_close() -> None:
    start = datetime(2021, 10, 12, 14, tzinfo=UTC)
    closes = pl.DataFrame(
        {
            "prediction_utc": [datetime(2021, 4, day, 23, tzinfo=UTC) for day in (1, 2, 3)]
            + [start, datetime(2021, 10, 13, 23, tzinfo=UTC)],
            "p_home": [0.236, 0.5, 0.837, 0.05, 0.99],
        }
    )
    # Closes at or after the fold's start are never read.
    assert bounds(closes, start) == (0.236, 0.837)
    with pytest.raises(ValueError, match="no closes"):
        bounds(closes, datetime(2021, 4, 1, tzinfo=UTC))


def test_an_opener_outside_the_bounds_is_implausible() -> None:
    odds, _ = market_history.seasons(SEASONS, games=20)
    assert not implausible(market_prices(odds, Experiment.E2), 0.2, 0.85).any()
    prices = market_prices(market_history.implausible_opener(odds, TESTED), Experiment.E2)
    assert prices.filter(implausible(prices, 0.2, 0.85))["game_id"].to_list() == [TESTED]
    assert not implausible(prices, 0.1, 0.9).any()
    # A market de-vigging refuses is refused, not implausible.
    below = prices.with_columns(home_price=pl.lit(2.5), away_price=pl.lit(2.5))
    assert not implausible(below, 0.2, 0.85).any()


def test_e2_refuses_an_implausible_opener_in_scoring_and_b1_fits() -> None:
    odds, games = market_history.seasons(SEASONS, games=200)
    for game_id in (TRAINING, TESTED):
        odds = market_history.implausible_opener(odds, game_id)
    refusing, coverage, fits = run(odds, games, [20182019])
    every, all_coverage, all_fits = run(odds, games, [20182019], refuse_implausible=False)
    counts, all_counts = coverage["E2"][20182019], all_coverage["E2"][20182019]
    assert (counts["implausible"], all_counts["implausible"]) == (1, 0)
    # The moved-opener count reads every opener, refused or not.
    assert counts["opener_differs_from_close"] == all_counts["opener_differs_from_close"]
    assert counts["priced"] == all_counts["priced"]
    assert counts["scored"] == all_counts["scored"] - 1
    assert fits["E2"][20182019].games == all_fits["E2"][20182019].games - 1
    e2 = refusing.filter(experiment="E2")
    assert TESTED not in e2["game_id"].to_list()
    assert TESTED in every.filter(experiment="E2")["game_id"].to_list()
    # E1 reads the close, so the rule never touches it.
    assert refusing.filter(experiment="E1", method="multiplicative").equals(
        every.filter(experiment="E1", method="multiplicative")
    )
    assert coverage["E1"] == all_coverage["E1"]
    assert fits["E1"] == all_fits["E1"]


def test_the_sensitivity_reports_e2_on_every_opener() -> None:
    odds, games = market_history.seasons(SEASONS, games=200)
    odds = market_history.implausible_opener(odds, TESTED)
    report = sensitivity.every_opener(odds, games, [20182019])[sensitivity.VARIANT]
    counts = report["E2"]["coverage"]["20182019"]
    assert (counts["implausible"], counts["scored"]) == (0, counts["priced"])
    assert list(report["E2"]["models"]["B0"]["log_loss"]) == ["multiplicative"]
    against_e1 = report["e2_against_e1"]["B0"]["multiplicative"]["pooled"]
    assert against_e1["games"] == counts["scored"]


def test_an_earlier_close_as_extreme_keeps_the_opener() -> None:
    # A close before the fold as extreme as the opener widens the fold's bounds, so the opener
    # counts as a market seen before (ADR 0007).
    odds, games = market_history.seasons(SEASONS, games=200)
    odds = market_history.implausible_opener(odds, TESTED)
    widened = market_history.implausible_opener(odds, TRAINING, quote="close")
    _, coverage, _ = run(odds, games, [20182019])
    _, widened_coverage, _ = run(widened, games, [20182019])
    assert coverage["E2"][20182019]["implausible"] == 1
    assert widened_coverage["E2"][20182019]["implausible"] == 0


def test_a_fold_never_depends_on_the_other_requested_seasons() -> None:
    # The 2020-21 fold refuses this opener, but its own close widens the 2021-22 fold's bounds, so
    # that fold's fit keeps it whether or not 2020-21 is requested too.
    odds, games = market_history.seasons([20192020, 20202021, 20212022], games=60)
    game_id = games.filter(season=20202021)["game_id"][0]
    odds = market_history.implausible_opener(odds, game_id)
    odds = market_history.implausible_opener(odds, game_id, quote="close")
    _, both_coverage, both = run(odds, games, [20202021, 20212022])
    _, _, alone = run(odds, games, [20212022])
    assert both_coverage["E2"][20202021]["implausible"] == 1
    assert both["E2"][20212022] == alone["E2"][20212022]
