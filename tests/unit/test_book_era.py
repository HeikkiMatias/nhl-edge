import market_history
import polars as pl

from nhl_edge.backtest import book_era

SEASONS = [20162017, 20172018, 20182019, 20192020]
FIRST, LATER = 2016020001, 2018020001
DRAWS = 50


def planted() -> tuple[pl.DataFrame, pl.DataFrame]:
    """Four synthetic seasons, two in each book era, with an implausible opener in the first
    season and one in the first season of the new closing book."""
    odds, games = market_history.seasons(SEASONS, games=120)
    for game_id in (FIRST, LATER):
        odds = market_history.implausible_opener(odds, game_id)
    return odds, games


def test_openers_are_refused_as_in_adr_0007_except_in_the_first_season() -> None:
    closes, openers = book_era.markets(*planted())
    # The first season has no earlier close to bound it; later seasons refuse the opener.
    assert FIRST in openers["game_id"].to_list()
    assert LATER not in openers["game_id"].to_list()
    assert {FIRST, LATER} <= set(closes["game_id"].to_list())
    assert closes.height == openers.height + 1


def test_the_eras_split_at_2018_19() -> None:
    report = book_era.diagnostic(*planted(), draws=DRAWS)
    cost = report["b0_e2_against_e1"]
    assert list(cost["per_season"]) == [str(s) for s in SEASONS]
    assert list(cost["eras"]) == ["same_book", "new_closing_book"]
    assert (cost["eras"]["same_book"]["games"], cost["eras"]["new_closing_book"]["games"]) == (
        240,
        239,
    )
    closes = report["b1_on_closes"]["eras"]
    assert closes["same_book"]["seasons"] == [20162017, 20172018]
    assert closes["new_closing_book"]["seasons"] == [20182019, 20192020]
    assert report["b1_on_openers"]["eras"]["new_closing_book"]["games"] == 239
    for spread in (cost["era_difference"], report["b1_on_closes"]["slope_difference"]):
        assert spread["low"] <= spread["value"] <= spread["high"]
    fit = closes["same_book"]["slope"]
    assert fit["low"] <= fit["value"] <= fit["high"]


def test_held_out_seasons_are_never_read() -> None:
    odds, games = planted()
    held_odds, held_games = market_history.seasons([20222023], games=60, intercept=2.0)
    with_held = book_era.diagnostic(
        pl.concat([odds, held_odds]), pl.concat([games, held_games]), draws=DRAWS
    )
    assert with_held == book_era.diagnostic(odds, games, draws=DRAWS)


def test_one_era_reports_no_difference_between_eras() -> None:
    odds, games = market_history.seasons(SEASONS[:2], games=120)
    report = book_era.diagnostic(odds, games, draws=DRAWS)
    assert list(report["b0_e2_against_e1"]["eras"]) == ["same_book"]
    assert "era_difference" not in report["b0_e2_against_e1"]
    assert "slope_difference" not in report["b1_on_closes"]
