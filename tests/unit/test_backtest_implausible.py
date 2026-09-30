import market_history
import polars as pl

from nhl_edge.backtest import implausible
from nhl_edge.backtest.walk_forward import run

SEASONS = [20172018, 20182019]
TRAINING = 2017020001
TESTED = 2018020002


def test_without_openers_removes_only_the_listed_moneyline_openers() -> None:
    odds, _ = market_history.seasons(SEASONS, games=20)
    kept = implausible.without_openers(odds, [TRAINING, TESTED])
    removed = odds.join(kept, on=["game_id", "market", "quote", "side"], how="anti")
    assert removed.height == 4
    assert set(removed["game_id"]) == {TRAINING, TESTED}
    assert set(removed["quote"]) == {"open"}
    assert kept.filter(pl.col("game_id") == TESTED, pl.col("quote") == "close").height == 2


def test_an_opener_outside_the_bounds_is_implausible() -> None:
    odds, _ = market_history.seasons(SEASONS, games=20)
    assert implausible.implausible(odds).is_empty()
    odds = market_history.implausible_opener(odds, TESTED)
    assert implausible.implausible(odds).rows() == [(TESTED, 20182019)]


def test_the_variant_drops_implausible_openers_from_e2_scoring_and_b1_fits() -> None:
    odds, games = market_history.seasons(SEASONS, games=200)
    for game_id in (TRAINING, TESTED):
        odds = market_history.implausible_opener(odds, game_id)
    _, coverage, fits = run(odds, games, [20182019])
    report = implausible.sensitivity(odds, games, [20182019])[implausible.VARIANT]
    assert report["removed_openers"] == {"20172018": 1, "20182019": 1}
    counts = report["E2"]["coverage"]["20182019"]
    assert counts["priced"] == coverage["E2"][20182019]["priced"] - 1
    assert counts["b1_trained_on"] == fits["E2"][20182019].games - 1
    assert list(report["E2"]["models"]["B0"]["log_loss"]) == ["multiplicative"]
    against_e1 = report["e2_against_e1"]["B0"]["multiplicative"]["pooled"]
    assert against_e1["games"] == counts["scored"]
