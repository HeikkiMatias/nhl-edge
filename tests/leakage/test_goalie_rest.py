"""Point in time for scripts/goalie_rest.py (#418, hard rules 1 and 9): a goalie's rest never
counts tonight's start, a start on tonight's date or one whose boxscore wasn't public before the
as-of time; later games change no earlier row; the game columns read only what was known at the
prediction; each shot-level season is fitted on earlier seasons only; and the script reads the
lake only to the explore half."""

import sys
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import goalie_rest_fixtures as f
import polars as pl
import pytest

from nhl_edge.backtest import yardstick as ys

gr = f.load()
D = date(2013, 11, 1)
TONIGHT = datetime.combine(D, time(15), UTC)


def test_rest_never_counts_tonight_or_a_boxscore_not_yet_public() -> None:
    ask = f.asks([(1, D, TONIGHT)])
    # Tonight's own start, and a start whose boxscore turns public after the as-of time.
    tonight = f.started([(1, D, 0)])
    late = f.started([(1, D - timedelta(days=1), 0)]).with_columns(
        start_utc=pl.lit(TONIGHT + timedelta(minutes=1), dtype=f.UTC_TYPE)
    )
    for started in (tonight, late):
        row = gr.rest(ask, started).row(0, named=True)
        assert row["days"] is None and row["starts7"] == 0 and row["yesterday"] == 0.0
    # The same start of yesterday, public by 10:00 UTC this morning, counts.
    on_time = gr.rest(ask, f.started([(1, D - timedelta(days=1), 0)])).row(0, named=True)
    assert on_time["yesterday"] == 1.0 and on_time["days"] == 1


def test_later_starts_change_no_earlier_row() -> None:
    before = f.started([(1, D - timedelta(days=d), 0) for d in (1, 3, 9)])
    later = pl.concat([before, f.started([(1, D + timedelta(days=d), 0) for d in (0, 1, 2)])])
    ask = f.asks([(1, D, TONIGHT)])
    assert gr.rest(ask, later).equals(gr.rest(ask, before))


def test_the_game_columns_read_only_what_was_known_at_the_prediction() -> None:
    read = f.league_read()
    columns, _ = gr.rest_columns(read)
    # With no boxscore public before any prediction, every column is 0 (rested and unknown).
    late = read.b2_tables.actual_lineups.with_columns(pl.col("observed_utc") + timedelta(days=400))
    blind, _ = gr.rest_columns(
        replace(read, b2_tables=replace(read.b2_tables, actual_lineups=late))
    )
    assert blind.height == columns.height
    for name in (*gr.WORKLOAD, *gr.B2B):
        assert blind[name].abs().max() == 0
    # A start probability observed after the prediction is never read: none known, none left.
    starts_late = read.b3_tables.goalie_starts.with_columns(
        pl.col("observed_utc") + timedelta(days=400)
    )
    unknown, missing = gr.rest_columns(
        replace(read, b3_tables=replace(read.b3_tables, goalie_starts=starts_late))
    )
    assert unknown.is_empty() and missing == columns.height


def test_the_game_columns_are_as_of_the_prediction_not_the_start() -> None:
    read = f.league_read()
    hours = (pl.col("game_date").cast(f.UTC_TYPE) + timedelta(hours=20)).alias("cut")
    frame = read.frame.with_columns(hours)
    # Every prediction comes before 20:00 UTC on its date, and every start after it.
    assert (frame["prediction_utc"] < frame["cut"]).all() and (
        frame["start_utc"] > frame["cut"]
    ).all()
    columns, _ = gr.rest_columns(read)
    assert columns["goalie_b2b_diff"].abs().max() > 0  # type: ignore[operator]
    # Yesterday's boxscores turn public at 20:00 UTC today: after the prediction, before the
    # start. A column read at the start would count them; at the prediction, none counts.
    lineups = read.b2_tables.actual_lineups.with_columns(
        observed_utc=pl.col("game_date").cast(f.UTC_TYPE) + timedelta(days=1, hours=20)
    )
    shifted, _ = gr.rest_columns(
        replace(read, b2_tables=replace(read.b2_tables, actual_lineups=lineups))
    )
    assert shifted["goalie_b2b_diff"].abs().max() == 0


def test_shot_rows_read_no_later_season_and_no_term_known_after_the_start() -> None:
    shots, xg = f.step1_shots()
    later = shots.with_columns(
        season=pl.lit(20142015, dtype=pl.Int32), game_id=pl.lit(99, dtype=pl.Int64)
    )
    rows, _ = gr.shot_rows(pl.concat([shots, later]), xg, f.step1_read())
    assert set(rows["game_id"]) == {2} and rows["season"].max() == 20132014
    # An xG fitted at or after its season's fold start is left out and counted (the starters'
    # two shots).
    refit = xg.with_columns(
        train_cutoff=pl.lit(datetime(2013, 11, 2, tzinfo=UTC), dtype=f.UTC_TYPE)
    )
    rows, counts = gr.shot_rows(shots, refit, f.step1_read())
    assert rows.is_empty() and counts["xg_fitted_at_or_after_the_fold_start"] == 2


def test_each_shot_season_is_fitted_on_earlier_seasons_public_before_its_fold_start() -> None:
    shots = f.shots()
    calendar = f.calendar(shots)
    scored = gr.shot_scores(shots, calendar)
    for season in ys.EXPLORE:
        start = ys.fold_start(calendar, season)
        cutoff = scored.filter(pl.col("season") == season)["train_cutoff"].max()
        assert isinstance(cutoff, datetime) and cutoff < start
    # Changing 2014-15's goals leaves 2013-14's probabilities as they were.
    flipped = shots.with_columns(
        goal=pl.when(pl.col("season") == 20142015).then(1 - pl.col("goal")).otherwise("goal")
    )
    first = pl.col("season") == min(ys.EXPLORE)
    again = gr.shot_scores(flipped, calendar).filter(first).select("p_base", "p_full")
    assert again.equals(scored.filter(first).select("p_base", "p_full"))


def test_the_script_reads_the_lake_only_to_the_explore_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[int] = []

    def read(lake: Any, last: int) -> None:
        asked.append(last)
        raise RuntimeError("stop here")

    monkeypatch.setattr(gr.ys, "read", read)
    monkeypatch.setattr(gr, "Lake", lambda: None)
    monkeypatch.setattr(sys, "argv", ["goalie_rest.py"])
    with pytest.raises(RuntimeError, match="stop here"):
        gr.main()
    assert asked == [max(ys.EXPLORE)]
