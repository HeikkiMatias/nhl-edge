"""scripts/upsets.py (#366): the runs of wins, losses and away games, each side's form from the
results public before the opener, the letdown spots in mismatches, and the calibration bands, on
games built by hand."""

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from upsets_fixtures import GAMES, UTC_TYPE, frame, load

up = load()


def test_the_runs_of_wins_losses_and_away_games() -> None:
    found = up.history(GAMES.filter(pl.col("game_id") <= 8))
    bos = found.filter(pl.col("team") == "BOS").sort("start_utc")
    assert bos["wins"].to_list() == [1, 2, 3, 4]
    assert bos["away"].to_list() == [0, 1, 2, 3]
    assert bos["margin"].to_list() == [1, 1, 1, 4]
    tor = found.filter(pl.col("team") == "TOR").sort("start_utc")
    assert tor["losses"].to_list() == [1, 2, 3, 4]
    assert tor["wins"].to_list() == [0, 0, 0, 0]


def test_form_reads_only_results_public_before_the_opener() -> None:
    found = up.form(frame(), GAMES).row(0, named=True)
    # BOS, away: four straight wins, the last by 4, and this its fourth straight away game.
    assert (found["away_wins"], found["away_margin"], found["away_road"]) == (4, 4, 4)
    assert (found["home_losses"], found["home_road"]) == (4, 0)
    # The night before's result is public at 10:00 UTC, before the opener; a later one is not.
    late = GAMES.with_columns(
        observed_utc=pl.when(pl.col("game_id") == 4)
        .then(pl.lit(datetime(2013, 10, 9, 15, tzinfo=UTC)).cast(UTC_TYPE))
        .otherwise(pl.col("observed_utc"))
    )
    early = up.form(frame(), late).row(0, named=True)
    assert (early["away_wins"], early["away_margin"]) == (3, 1)


def test_the_spots_hold_only_in_mismatches() -> None:
    # BOS, the away team, is the favourite at 0.70 by the opener.
    found = up.spots(frame(0.30, 0.32), GAMES).row(0, named=True)
    assert not found["home_favourite"]
    assert found["fav_open"] == pytest.approx(0.70) and found["fav_close"] == pytest.approx(0.68)
    assert found["fav_won"] == 1
    assert all(found[spot] for spot in up.SPOTS)
    # Near even at the opener, no spot holds.
    even = up.spots(frame(0.45, 0.45), GAMES).row(0, named=True)
    assert not any(even[spot] for spot in up.SPOTS)


def test_the_calibration_bands_compare_win_rates_with_probabilities() -> None:
    days = [date(2013, 10, 1) + timedelta(days=7 * (i % 5)) for i in range(40)]
    rows = pl.DataFrame(
        {
            "season": [20132014] * 40,
            "game_date": days,
            # Home favourites at 0.75 that win half the time, and away favourites at 0.62.
            "p_close": [0.75] * 20 + [0.38] * 20,
            "home_win": [i % 2 for i in range(20)] + [0] * 20,
        }
    ).with_columns(pl.col("season").cast(pl.Int32), pl.col("home_win").cast(pl.Int8))
    found = up.calibration(rows, "p_close")
    assert found["0.70 to 1.00"]["games"] == 20
    assert found["0.70 to 1.00"]["gap"]["mean"] == pytest.approx(0.5 - 0.75)
    assert found["0.60 to 0.65"]["won"] == pytest.approx(1.0)
    assert found["every favourite"]["games"] == 40
