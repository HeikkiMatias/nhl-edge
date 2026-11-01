"""Point in time for scripts/upsets.py (#366, hard rule 1): each side's form reads only results
public before the opener and within its season, the spots are chosen by the opener and never the
close, the model side is fitted on earlier seasons' results public before the fold start and
never on 2011-12, and nothing reads the confirm half."""

import sys
from datetime import UTC, datetime
from typing import Any

import numpy as np
import polars as pl
import pytest
import yardstick_fixtures as yf
from upsets_fixtures import GAMES, UTC_TYPE, frame, load

from nhl_edge.backtest import yardstick as ys

up = load()


def _restamp(games: pl.DataFrame, game_id: int, when: datetime) -> pl.DataFrame:
    return games.with_columns(
        observed_utc=pl.when(pl.col("game_id") == game_id)
        .then(pl.lit(when).cast(UTC_TYPE))
        .otherwise(pl.col("observed_utc"))
    )


def test_a_result_public_at_the_opener_is_not_known_yet() -> None:
    # BOS's fourth straight win (game 4, by 4) made public at the prediction time itself.
    found = up.form(frame(), _restamp(GAMES, 4, datetime(2013, 10, 9, 14, tzinfo=UTC)))
    row = found.row(0, named=True)
    assert (row["away_wins"], row["away_margin"]) == (3, 1)


def test_runs_stop_at_the_season_edge() -> None:
    # The same results, a season earlier: none carries into game 9's season.
    earlier = GAMES.with_columns(pl.lit(20122013).cast(pl.Int32).alias("season"))
    row = up.form(frame(), earlier).row(0, named=True)
    assert (row["away_wins"], row["away_margin"], row["away_road"]) == (0, None, 1)
    assert row["home_losses"] == 0


def test_the_spots_are_chosen_by_the_opener_not_the_close() -> None:
    # A mismatch only by the close flags nothing; one only by the opener flags every spot.
    by_close = up.spots(frame(0.45, 0.25), GAMES).row(0, named=True)
    assert not any(by_close[spot] for spot in up.SPOTS)
    by_open = up.spots(frame(0.30, 0.48), GAMES).row(0, named=True)
    assert all(by_open[spot] for spot in up.SPOTS)


def _model_rows() -> pl.DataFrame:
    """Yardstick rows from 2011-12 to the confirm half, with the model side's two inputs."""
    return yf.moves((*yf.SEASONS, *ys.CONFIRM)).with_columns(
        delta_g_hat=pl.col("c"), h_s=pl.col("w")
    )


def _flip(rows: pl.DataFrame, where: pl.Expr) -> pl.DataFrame:
    return rows.with_columns(
        home_win=pl.when(where).then(1 - pl.col("home_win")).otherwise(pl.col("home_win"))
    )


def _p(rows: pl.DataFrame, seasons: tuple[int, ...]) -> np.ndarray:
    return up.model_side(rows, seasons).sort("game_id")["p_model"].to_numpy()


def test_the_model_side_never_reads_a_later_season() -> None:
    rows = _model_rows()
    flipped = _flip(rows, pl.col("season") >= 20142015)
    assert np.array_equal(_p(rows, (20132014,)), _p(flipped, (20132014,)))
    # Flipping 2013-14 itself does move 2014-15's fit, so the check has power.
    assert not np.allclose(
        _p(rows, (20142015,)), _p(_flip(rows, pl.col("season") == 20132014), (20142015,))
    )


def test_the_model_side_drops_a_result_public_after_its_fold_start() -> None:
    rows = _model_rows()
    first = int(rows.filter(pl.col("season") == 20132014)["game_id"][0])
    late = rows.with_columns(
        result_utc=pl.when(pl.col("game_id") == first)
        .then(pl.lit(datetime(2015, 6, 1, tzinfo=UTC)).cast(UTC_TYPE))
        .otherwise(pl.col("result_utc"))
    )
    assert np.array_equal(
        _p(late, (20142015,)), _p(_flip(late, pl.col("game_id") == first), (20142015,))
    )


def test_the_model_side_never_fits_on_2011_12() -> None:
    rows = _model_rows()
    assert np.array_equal(
        _p(rows, ys.EXPLORE), _p(_flip(rows, pl.col("season") == ys.FIRST_READ), ys.EXPLORE)
    )
    # Without 2012-13 there is nothing to fit 2013-14 on, and it says so.
    with pytest.raises(ValueError, match="no results from 20122013"):
        up.model_side(rows.filter(pl.col("season") != 20122013), (20132014,))


def test_the_model_side_and_the_spots_never_read_the_confirm_half() -> None:
    rows = _model_rows().with_columns(p_open=0.5 + pl.col("o") / 10, p_close=0.5)
    noisy = _flip(rows, pl.col("season").is_in(list(ys.CONFIRM))).with_columns(
        y=pl.when(pl.col("season").is_in(list(ys.CONFIRM)))
        .then(pl.col("y") * 10)
        .otherwise(pl.col("y"))
    )
    assert np.array_equal(_p(rows, ys.EXPLORE), _p(noisy, ys.EXPLORE))

    def flags(source: pl.DataFrame) -> pl.DataFrame:
        # Every fifth game a home favourite at 0.65 in every spot.
        spot = (pl.int_range(pl.len()) % 5) == 0
        return source.select(
            "game_id",
            "season",
            "game_date",
            home_favourite=pl.lit(True),
            fav_open=pl.lit(0.65),
            fav_close=pl.lit(0.6),
            fav_won=pl.col("home_win"),
            **{name: spot for name in up.SPOTS},
        )

    assert up.letdowns(rows, flags(rows)) == up.letdowns(noisy, flags(noisy))


def test_the_script_reads_the_lake_only_to_the_explore_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[int] = []

    def read(lake: Any, last: int) -> None:
        asked.append(last)
        raise RuntimeError("stop here")

    monkeypatch.setattr(up.ys, "read", read)
    monkeypatch.setattr(up, "Lake", lambda: None)
    monkeypatch.setattr(sys, "argv", ["upsets.py"])
    with pytest.raises(RuntimeError, match="stop here"):
        up.main()
    assert asked == [max(ys.EXPLORE)] and max(ys.EXPLORE) < min(ys.CONFIRM)
