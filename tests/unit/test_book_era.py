import market_history

from nhl_edge.backtest import book_era

SEASONS = [20162017, 20172018, 20182019, 20192020]
DRAWS = 50


def test_every_season_with_an_earlier_one_is_a_fold() -> None:
    odds, _ = market_history.seasons(SEASONS, games=60)
    assert book_era.seasons_of(odds) == SEASONS[1:]


def test_the_eras_split_at_2018_19() -> None:
    odds, games = market_history.seasons(SEASONS, games=120)
    report = book_era.diagnostic(odds, games, draws=DRAWS)
    assert report["seasons"] == [20172018, 20182019, 20192020]
    cost = report["b0_e2_minus_e1"]
    assert list(cost["per_season"]) == ["20172018", "20182019", "20192020"]
    assert list(cost["eras"]) == ["same_book", "new_closing_book"]
    assert (cost["eras"]["same_book"]["games"], cost["eras"]["new_closing_book"]["games"]) == (
        120,
        240,
    )
    for experiment in ("E1", "E2"):
        gain = report["b0_minus_b1"][experiment]
        spread = gain["era_difference"]
        assert spread["low"] <= spread["value"] <= spread["high"]
        # B1 is fitted on every earlier season: 120 games for 2017-18, 360 for 2019-20.
        fits = report["b1_fits"][experiment]
        assert [fits[str(s)]["games"] for s in SEASONS[1:]] == [120, 240, 360]


def test_one_era_reports_no_difference_between_eras() -> None:
    odds, games = market_history.seasons(SEASONS[:2], games=120)
    report = book_era.diagnostic(odds, games, draws=DRAWS)
    assert list(report["b0_e2_minus_e1"]["eras"]) == ["same_book"]
    assert "era_difference" not in report["b0_e2_minus_e1"]
    assert "era_difference" not in report["b0_minus_b1"]["E1"]
