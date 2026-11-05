"""Point in time for scripts/totals_history.py (#359, hard rule 1): the projection reads only the
inputs and goalie candidates observed before the prediction time, the totals opener only when
assumed available before it, the close never enters the move's forecast, each season's fits read
only games whose results were public before its fold start, and an explore run reads no confirm
season. SYNTHETIC data."""

import sys
from datetime import timedelta

import polars as pl
import pytest
from totals_history_fixtures import (
    OPENED,
    PREDICTION,
    game_quotes,
    gammas,
    load,
    moments,
    pool,
    sbr,
    scored_frame,
    split,
)

from nhl_edge.backtest import yardstick as ys

th = load()
SCORED = (20132014, 20142015)


def test_inputs_observed_at_the_prediction_time_are_never_read() -> None:
    late = split(observed=PREDICTION)
    assert th.projection(late, pool(), gammas(), moments([1])).is_empty()


def test_a_goalie_candidate_observed_after_the_prediction_is_never_read() -> None:
    base = th.projection(split(), pool(), gammas(), moments([1]))
    later = pool([(1, "BOS", 99, 1.0)], observed=PREDICTION + timedelta(seconds=1))
    every = pl.concat([pool(), later])
    shocked = gammas([(1, "NYR", 99, 5.0)])
    found = th.projection(split(), every, pl.concat([gammas(), shocked]), moments([1]))
    assert found["total"].to_list() == pytest.approx(base["total"].to_list())


def test_an_opener_assumed_at_or_after_the_prediction_is_never_read() -> None:
    late = game_quotes(1, opened=PREDICTION)
    assert th.market(sbr(late), moments([1])).is_empty()
    early = game_quotes(1, opened=OPENED)
    assert th.market(sbr(early), moments([1])).height == 1


def test_the_close_never_enters_the_moves_forecast() -> None:
    frame = scored_frame()
    base = th.scored(frame, ("total",), SCORED)
    tested = pl.col("season").is_in(SCORED)
    moved = frame.with_columns(
        mu_c=pl.when(tested).then(pl.col("mu_c") + 3.0).otherwise(pl.col("mu_c")),
        q_c=pl.when(tested).then(-pl.col("q_c")).otherwise(pl.col("q_c")),
        y=pl.when(tested).then(pl.col("y") + 3.0).otherwise(pl.col("y")),
    )
    found = th.scored(moved, ("total",), SCORED)
    # The scored seasons' own closes change their targets, never the forecasts. The second
    # scored season's fit reads the first one's closes, so only the first is compared.
    first = pl.col("season") == SCORED[0]
    for column in ("y_hat", "y_base"):
        assert found.filter(first)[column].to_list() == pytest.approx(
            base.filter(first)[column].to_list()
        )


def test_each_season_fits_only_on_results_public_before_its_fold_start() -> None:
    frame = scored_frame()
    base = th.scored(frame, ("total",), SCORED)
    for season in SCORED:
        start = ys.fold_start(frame, season)
        cutoff = base.filter(pl.col("season") == season)["train_cutoff"].max()
        assert cutoff is not None and cutoff < start  # type: ignore[operator]
    # A game of an earlier season whose result was public only after the fold start is not read.
    first = SCORED[0]
    start = ys.fold_start(frame, first)
    game = frame.filter(pl.col("season") == SCORED[0] - 10001)["game_id"].max()
    this = pl.col("game_id") == game
    late = frame.with_columns(
        result_utc=pl.when(this).then(pl.lit(start, dtype=th.UTC_TYPE)).otherwise("result_utc"),
        y=pl.when(this).then(pl.col("y") + 50.0).otherwise("y"),
    )
    found = th.scored(late, ("total",), (first,))
    expected = th.scored(frame.filter(~this), ("total",), (first,))
    assert found["y_hat"].to_list() == pytest.approx(expected["y_hat"].to_list())


def test_an_explore_run_reads_no_confirm_season() -> None:
    frame = scored_frame()
    _, base = th.run(frame, "explore")
    confirm = pl.col("season") > max(SCORED)
    assert frame.filter(confirm).height > 0
    shocked = frame.with_columns(
        y=pl.when(confirm).then(pl.col("y") * 100).otherwise(pl.col("y")),
        over=pl.when(confirm).then(1 - pl.col("over")).otherwise(pl.col("over")),
    )
    _, found = th.run(shocked, "explore")
    for name in th.CANDIDATES:
        for measure in th.MEASURES:
            assert found[name][measure]["mean"] == pytest.approx(base[name][measure]["mean"])


def test_a_confirm_run_is_refused_without_a_written_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["totals_history.py", "--half", "confirm"])
    monkeypatch.setattr(th, "CLAIMS", ())
    with pytest.raises(SystemExit, match="no claim"):
        th.main()
