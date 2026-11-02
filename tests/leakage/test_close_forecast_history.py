"""Point in time for scripts/close_forecast_history.py (#369, hard rule 1): it reads the lake and
SBR's prices only to the last training season, and the simpler form's outcome model and its k
are fitted per season on earlier seasons only."""

import sys
from typing import Any

import numpy as np
import polars as pl
import pytest
import yardstick_fixtures as yf
from close_forecast_fixtures import load, with_parts

from nhl_edge.backtest import yardstick as ys

cf = load()


def test_the_script_reads_the_lake_only_to_the_training_seasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[int] = []

    def read(lake: Any, last: int) -> None:
        asked.append(last)
        raise RuntimeError("stop here")

    monkeypatch.setattr(cf.ys, "read", read)
    monkeypatch.setattr(cf, "Lake", lambda: None)
    monkeypatch.setattr(sys, "argv", ["close_forecast_history.py"])
    with pytest.raises(RuntimeError, match="stop here"):
        cf.main()
    assert asked == [max(ys.CONFIRM)]


def test_sbrs_prices_are_read_only_to_the_last_training_season() -> None:
    class Prices:
        def read(self, table: str) -> pl.DataFrame:
            assert table == "sbr_odds"
            return pl.DataFrame(
                {
                    "game_id": [1, 1, 2, 2],
                    "season": [20172018, 20172018, 20182019, 20182019],
                    "market": ["h2h"] * 4,
                    "quote": ["open"] * 4,
                    "side": ["home", "away"] * 2,
                    "price_decimal": [1.9, 2.0, 1.8, 2.1],
                }
            )

    found = cf.opener_prices(Prices(), max(ys.CONFIRM))  # type: ignore[arg-type]
    assert found["game_id"].to_list() == [1]


def test_the_simpler_form_never_reads_a_later_season() -> None:
    frame = with_parts(yf.moves((*yf.SEASONS, 20152016, 20162017)))
    later = pl.col("season") >= 20152016
    noisy = frame.with_columns(
        home_win=pl.when(later).then(1 - pl.col("home_win")).otherwise(pl.col("home_win")),
        y=pl.when(later).then(pl.col("y") * 10).otherwise(pl.col("y")),
    )

    def scored(rows: pl.DataFrame) -> np.ndarray:
        found = cf.shrink(rows, (20142015,))
        return found.sort("game_id")["s_shrink"].to_numpy()

    assert np.array_equal(scored(frame), scored(noisy))
