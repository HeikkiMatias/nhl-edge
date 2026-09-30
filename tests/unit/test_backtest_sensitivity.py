import market_history
import polars as pl

from nhl_edge.backtest import sensitivity
from nhl_edge.backtest.market import Experiment, market_prices
from nhl_edge.backtest.walk_forward import implausible, run

SEASONS = [20172018, 20182019]
TRAINING = 2017020001
TESTED = 2018020002


def test_an_opener_outside_the_bounds_is_implausible() -> None:
    odds, _ = market_history.seasons(SEASONS, games=20)
    assert not implausible(market_prices(odds, Experiment.E2)).any()
    prices = market_prices(market_history.implausible_opener(odds, TESTED), Experiment.E2)
    assert prices.filter(implausible(prices))["game_id"].to_list() == [TESTED]
    # A market de-vigging refuses is refused, not implausible.
    below = prices.with_columns(home_price=pl.lit(2.5), away_price=pl.lit(2.5))
    assert not implausible(below).any()


def test_e2_refuses_an_implausible_opener_in_scoring_and_b1_fits() -> None:
    odds, games = market_history.seasons(SEASONS, games=200)
    for game_id in (TRAINING, TESTED):
        odds = market_history.implausible_opener(odds, game_id)
    refusing, coverage, fits = run(odds, games, [20182019])
    every, all_coverage, all_fits = run(odds, games, [20182019], refuse_implausible=False)
    counts, all_counts = coverage["E2"][20182019], all_coverage["E2"][20182019]
    assert (counts["implausible"], all_counts["implausible"]) == (1, 0)
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
