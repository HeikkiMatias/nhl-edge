import market_history
import polars as pl

from nhl_edge.backtest import suspect
from nhl_edge.backtest.walk_forward import run

SEASONS = [20172018, 20182019]
# The first games of each synthetic season.
TRAINING = [2017020001, 2017020002, 2017020003]
TESTED = [2018020001, 2018020002]


def test_without_openers_removes_only_the_listed_moneyline_openers() -> None:
    odds, _ = market_history.seasons(SEASONS, games=20)
    kept = suspect.without_openers(odds, TESTED)
    removed = odds.join(kept, on=["game_id", "market", "quote", "side"], how="anti")
    assert removed.height == 4
    assert set(removed["game_id"]) == set(TESTED)
    assert set(removed["quote"]) == {"open"}
    assert kept.filter(pl.col("game_id").is_in(TESTED), pl.col("quote") == "close").height == 4


def test_each_variant_selects_its_flags() -> None:
    listed = market_history.suspects(
        {1: "big_move", 2: "extreme_open", 3: "swapped", 4: "below_100"}
    )
    every = suspect.listed(listed, "without_suspect_openers")
    proven = suspect.listed(listed, "without_proven_errors")
    assert every["game_id"].to_list() == [1, 2, 3, 4]
    assert proven["game_id"].to_list() == [2, 3, 4]


def test_a_variant_drops_its_openers_from_e2_scoring_and_b1_fits_only() -> None:
    odds, games = market_history.seasons(SEASONS, games=200)
    _, coverage, fits = run(odds, games, [20182019])
    listed = market_history.suspects(dict.fromkeys(TRAINING + TESTED, "big_move"))
    report = suspect.sensitivities(odds, games, [20182019], listed)

    dropped = report["without_suspect_openers"]
    assert dropped["removed_openers"] == {"20172018": 3, "20182019": 2}
    counts = dropped["E2"]["coverage"]["20182019"]
    assert counts["priced"] == coverage["E2"][20182019]["priced"] - 2
    assert counts["b1_trained_on"] == fits["E2"][20182019].games - 3
    assert dropped["E2"]["models"]["B1"]["fits"]["20182019"]["games"] == 197
    assert list(dropped["E2"]["models"]["B0"]["log_loss"]) == ["multiplicative"]
    against_e1 = dropped["e2_against_e1"]["B0"]["multiplicative"]["pooled"]
    assert against_e1["games"] == counts["scored"]

    # Big moves alone are not proven errors, so this variant removes nothing.
    kept = report["without_proven_errors"]
    assert kept["removed_openers"] == {}
    assert kept["E2"]["coverage"]["20182019"] == coverage["E2"][20182019]
