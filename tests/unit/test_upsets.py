"""scripts/upsets.py (#366): the runs of wins, losses and away games, each side's form from the
results public before the opener, the letdown spots in mismatches, and the calibration bands, on
games built by hand."""

import importlib.util
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import ModuleType

import polars as pl
import pytest

UTC_TYPE = pl.Datetime("us", "UTC")


def _load() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "upsets.py"
    spec = importlib.util.spec_from_file_location("upsets", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


up = _load()


def games(rows: list[tuple[int, int, str, str, int, int]]) -> pl.DataFrame:
    """(game_id, day of October 2013, home, away, home score, away score): each result public at
    10:00 UTC the next morning."""
    days = [date(2013, 10, r[1]) for r in rows]
    return pl.DataFrame(
        {
            "game_id": [r[0] for r in rows],
            "season": [20132014] * len(rows),
            "start_utc": [datetime.combine(d, time(23), UTC) for d in days],
            "home": [r[2] for r in rows],
            "away": [r[3] for r in rows],
            "home_score": [r[4] for r in rows],
            "away_score": [r[5] for r in rows],
            "observed_utc": [datetime.combine(d + timedelta(days=1), time(10), UTC) for d in days],
        }
    ).with_columns(
        pl.col("season").cast(pl.Int32),
        pl.col("home_score", "away_score").cast(pl.Int16),
        pl.col("start_utc", "observed_utc").cast(UTC_TYPE),
    )


# BOS wins four in a row, the last by 4, all but the first away; TOR loses four in a row.
GAMES = games(
    [
        (1, 1, "BOS", "MTL", 3, 2),
        (2, 3, "NYR", "BOS", 1, 2),
        (3, 5, "CHI", "BOS", 0, 1),
        (4, 7, "DET", "BOS", 1, 5),
        (5, 2, "TOR", "MTL", 1, 4),
        (6, 4, "TOR", "NYR", 0, 2),
        (7, 6, "CHI", "TOR", 3, 1),
        (8, 8, "DET", "TOR", 2, 0),
        (9, 9, "TOR", "BOS", 0, 0),
    ]
)


def frame(p_open: float = 0.30, p_close: float = 0.30) -> pl.DataFrame:
    """The yardstick's row for game 9, TOR at home against BOS, its opener at 14:00 UTC."""
    return pl.DataFrame(
        {
            "game_id": [9],
            "season": [20132014],
            "game_date": [date(2013, 10, 9)],
            "prediction_utc": [datetime(2013, 10, 9, 14, tzinfo=UTC)],
            "p_open": [p_open],
            "p_close": [p_close],
            "home_win": [0],
        }
    ).with_columns(
        pl.col("season").cast(pl.Int32),
        pl.col("home_win").cast(pl.Int8),
        pl.col("prediction_utc").cast(UTC_TYPE),
    )


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
