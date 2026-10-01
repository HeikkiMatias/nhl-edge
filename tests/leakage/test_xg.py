"""Point-in-time rules for xG (#73, ADR 0010, hard rule 1). A season's shots are scored by a model
fitted only on earlier seasons' shots public before the season's first game: no scored shot, nor
any later one, nor an earlier shot that became public after that first game, can move the xG of
a scored shot. Every shot_xg row carries a train_cutoff before its fold's start."""

from datetime import timedelta

import polars as pl
from shot_fixtures import games_of, synthetic_shots

from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.features import xg

SEASONS = [20102011, 20112012, 20122013, 20132014]
SHOTS = synthetic_shots(SEASONS, per_season=3000)
GAMES = games_of(SEASONS)
VERSION = "xg-20260930-abc1234"
SCORED = 20122013


def xg_of(shots: pl.DataFrame) -> pl.DataFrame:
    scored, _ = xg.score(shots, GAMES, [SCORED], VERSION)
    return scored.select("game_id", "event_id", "xg")


def flip_goals(shots: pl.DataFrame, rows: pl.Expr) -> pl.DataFrame:
    """Every goal in rows becomes a save and every save a goal."""
    flipped = pl.when(rows).then(~pl.col("is_goal")).otherwise(pl.col("is_goal"))
    return shots.with_columns(is_goal=flipped).with_columns(
        event_type=pl.when(pl.col("is_goal")).then(pl.lit("goal")).otherwise(pl.lit("shot-on-goal"))
    )


BEFORE = xg_of(SHOTS)


def test_the_scored_season_and_later_ones_never_move_its_xg() -> None:
    changed = flip_goals(SHOTS, pl.col("season") >= SCORED)
    assert xg_of(changed).equals(BEFORE)


def test_an_earlier_shot_public_only_after_the_fold_start_is_not_trained_on() -> None:
    # 2011-12's late-season shots, as if ingested only after 2012-13 began: their outcomes are
    # flipped, and still nothing moves.
    start = fold_start(GAMES, SCORED)
    late = (pl.col("season") == 20112012) & (pl.col("game_date").dt.month().is_in([2, 3]))
    changed = flip_goals(
        SHOTS.with_columns(
            observed_utc=pl.when(late)
            .then(pl.lit(start + timedelta(days=1)))
            .otherwise(pl.col("observed_utc"))
        ),
        late,
    )
    assert xg_of(changed).equals(xg_of(SHOTS.filter(~late)))


def test_an_earlier_public_shot_is_trained_on() -> None:
    # The guard above is not vacuous: flipping public earlier shots does move the xG.
    changed = flip_goals(SHOTS, pl.col("season") == 20112012)
    assert not xg_of(changed).equals(BEFORE)


def test_every_row_is_fitted_before_its_seasons_first_game() -> None:
    scored, models = xg.score(SHOTS, GAMES, SEASONS[1:], VERSION)
    for model in models:
        start = fold_start(GAMES, model.season)
        rows = scored.filter(pl.col("season") == model.season)
        assert model.train_cutoff < start
        assert (rows["train_cutoff"] < start).all()
        assert (rows["observed_utc"] > start).all()
