"""Point-in-time rules for the market-relative yardstick (#361, ADR 0053, hard rule 1): the close
is never an input to the move's forecast, every fit reads only results public before its season,
an explore run never reads the confirm half, each feature is as known at the opener's assumed
time, and only the tuning cutoff of the training seasons' frozen settings is set aside (ADR
0011)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest
import yardstick_fixtures as yf

from nhl_edge.backtest import yardstick as ys
from nhl_edge.backtest.seasons import TRAINING_SEASONS
from nhl_edge.features.team_strength import TUNED_CUTOFF


def test_the_close_never_enters_the_moves_forecast() -> None:
    frame = yf.moves()
    game = frame.filter(pl.col("season") == 20132014)["game_id"][5]
    one = pl.col("game_id") == game
    moved = frame.with_columns(
        c=pl.when(one).then(pl.col("c") + 1).otherwise(pl.col("c")),
        y=pl.when(one).then(pl.col("y") + 1).otherwise(pl.col("y")),
    )
    before = ys.scored(frame, ("x",), ys.EXPLORE).filter(pl.col("game_id") == game)
    after = ys.scored(moved, ("x",), ys.EXPLORE).filter(pl.col("game_id") == game)
    assert after["y_hat"].to_list() == before["y_hat"].to_list()
    assert after["y_base"].to_list() == before["y_base"].to_list()
    assert after["y"].item() == pytest.approx(before["y"].item() + 1)


def test_a_fit_reads_only_results_public_before_its_season() -> None:
    frame = yf.moves()
    start = frame.filter(pl.col("season") == 20132014)["start_utc"].min()
    assert isinstance(start, datetime)
    # The previous season's last games, their results made public only after the season starts,
    # and changed: no forecast of the season moves.
    late = frame.filter(pl.col("season") == 20122013).sort("game_id").tail(20)["game_id"]
    changed = frame.with_columns(
        result_utc=pl.when(pl.col("game_id").is_in(late.implode()))
        .then(pl.lit(start + timedelta(hours=1)).cast(ys.UTC))
        .otherwise(pl.col("result_utc"))
    )
    flipped = changed.with_columns(
        home_win=pl.when(pl.col("game_id").is_in(late.implode()))
        .then(1 - pl.col("home_win"))
        .otherwise(pl.col("home_win")),
        y=pl.when(pl.col("game_id").is_in(late.implode()))
        .then(-pl.col("y"))
        .otherwise(pl.col("y")),
    )
    season = pl.col("season") == 20132014
    a = ys.scored(changed, ("x",), (20132014,)).filter(season)
    b = ys.scored(flipped, ("x",), (20132014,)).filter(season)
    assert a.select("y_hat", "y_base", "p", "p_base").equals(
        b.select("y_hat", "y_base", "p", "p_base")
    )


def test_an_explore_run_never_reads_the_confirm_half() -> None:
    seasons = (*yf.SEASONS, *ys.CONFIRM)
    frame = yf.moves(seasons)
    noisy = frame.with_columns(
        y=pl.when(pl.col("season").is_in(list(ys.CONFIRM)))
        .then(pl.col("y") * 10)
        .otherwise(pl.col("y"))
    )
    plain, _ = ys.run(frame, "explore", {"x": ("x",)})
    other, _ = ys.run(noisy, "explore", {"x": ("x",)})
    assert plain.equals(other)
    assert set(plain["season"].unique().to_list()) == set(ys.EXPLORE)


def test_only_training_seasons_are_scored() -> None:
    assert set(ys.EXPLORE) | set(ys.CONFIRM) <= set(TRAINING_SEASONS)
    assert not set(ys.EXPLORE) & set(ys.CONFIRM)
    assert ys.FIRST_FIT not in ys.EXPLORE
    with pytest.raises(KeyError):
        ys.seasons_of("development")


def test_each_feature_is_as_known_at_the_opener_time() -> None:
    teams, players, sbr = yf.league()
    teams, players = ys.through(teams, 20142015), ys.through(players, 20142015)
    market = ys.market(sbr, players.games)
    moments = market.select("game_id", "prediction_utc")
    assert ys.features(teams, players, moments).height == market.height
    # A moment at the features' own as-of time, before the opener's, finds none known yet.
    early = moments.with_columns(pl.col("prediction_utc") - timedelta(seconds=1))
    assert ys.features(teams, players, early).is_empty()
    # The opener itself is read only after its assumed time (backtest/market.py).
    opens = sbr.filter(pl.col("quote") == "open", pl.col("market") == "h2h")
    assumed = opens.group_by("game_id").agg(pl.col("assumed_available_utc").max())
    joined = market.join(assumed, on="game_id")
    assert (joined["prediction_utc"] > joined["assumed_available_utc"]).all()


def test_every_fit_is_cut_before_its_seasons_fold_start() -> None:
    frame = yf.moves()
    per_game, _ = ys.run(frame, "explore", {"x": ("x",)})
    starts = {season: ys.fold_start(frame, season) for season in ys.EXPLORE}
    for season, cutoff in per_game.select("season", "train_cutoff").unique().iter_rows():
        assert cutoff < starts[season]
    # E2's fold starts at its first opener when that comes before the first start.
    early = frame.with_columns(prediction_utc=pl.col("start_utc") - timedelta(hours=9))
    first = early.filter(pl.col("season") == 20132014)["start_utc"].min()
    assert isinstance(first, datetime)
    assert ys.fold_start(early, 20132014) == first - timedelta(hours=9)


def test_a_fit_after_the_tuning_cutoff_is_refused() -> None:
    teams, players, _ = yf.league()
    late = players.lineups.with_columns(train_cutoff=pl.col("observed_utc") + timedelta(hours=1))
    with pytest.raises(ValueError, match="fitted after the tuning cutoff"):
        ys.through(replace(players, lineups=late), 20142015)
    later = teams.goalie_effects.with_columns(
        train_cutoff=pl.lit(TUNED_CUTOFF + timedelta(days=1)).cast(ys.UTC)
    )
    with pytest.raises(ValueError, match="fitted after the tuning cutoff"):
        ys.through(replace(teams, goalie_effects=later), 20142015)


def test_only_the_tuning_cutoff_is_set_aside() -> None:
    as_of = datetime(2013, 11, 1, 14, tzinfo=UTC)
    lifted = pl.DataFrame({"as_of_utc": [as_of], "observed_utc": [as_of + timedelta(days=3)]})
    with pytest.raises(ValueError, match="neither at their data's time nor the cutoff"):
        ys.untuned(lifted.with_columns(pl.all().cast(ys.UTC)))
    # A row observed before its data was known is refused.
    later = datetime(2018, 11, 1, 14, tzinfo=UTC)
    known = pl.DataFrame(
        {"as_of_utc": [later], "known_utc": [later + timedelta(hours=1)], "observed_utc": [later]}
    ).with_columns(pl.all().cast(ys.UTC))
    with pytest.raises(ValueError):
        ys.untuned(known)
    # Tables cut to the half drop every later season's rows.
    _, players, _ = yf.league()
    cut = ys.through(players, 20122013)
    assert cut.player_ratings["season"].max() == 20122013
