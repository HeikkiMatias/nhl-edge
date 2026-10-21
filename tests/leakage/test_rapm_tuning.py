"""Point-in-time rules for RAPM's tuning (#103, ADR 0011, hard rule 1).

Aging shifts a player's past evidence at each season's start by his age curve, which reads only
seasons that ended before it: the season's own stints never move its opening night, and a game's
own night's and later stints never move its ratings.

The tuning's feature, a game's projected 5v5 expected-goal difference, reads only that game's
ratings and projected lineup: later games' ratings and lineups, its own boxscore roles and its
own night's and later stints never move it."""

from functools import cache

import numpy as np
import polars as pl
import pytest
import rapm_fixtures as fx
from numpy.typing import NDArray

from nhl_edge.ratings import priors as pr
from nhl_edge.ratings import rapm

THREE = (*fx.SEASONS, 20132014)
LONG = fx.league(seasons=THREE)
GAMES, STINTS, ROLES, VENUES = (LONG[k] for k in ("games", "stints", "roles", "venues"))
VERSION = "rapm-20261003-abc1234"
AGED = rapm.Settings(half_life_days=180.0, pull_hours=2.0, aging=1.0)
# The third season is the first with age curves, from the first two's season ends.
SEASON = THREE[2]
DATES = GAMES.filter(pl.col("season") == SEASON)["game_date"].unique().sort()
OPENING, NIGHT = DATES[0], DATES[10]
WANTED = rapm.targets(LONG["lineups"], GAMES, [SEASON]).filter(
    pl.col("game_date").is_in([OPENING, NIGHT])
)
# A projected lineup for every game, with each skater's expected 5v5 minutes.
LINEUPS = LONG["lineups"].with_columns(exp_5v5=10.0 + pl.col("player_id") % 9)


@pytest.fixture(autouse=True)
def low_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fixture's players have a few hours a season; let them all count toward the curves.
    monkeypatch.setattr(pr, "AGE_HOURS", 0.5)
    monkeypatch.setattr(pr, "AGE_PP_HOURS", 0.1)


def doubled(where: pl.Expr) -> pl.DataFrame:
    """STINTS with the xG of those where holds doubled."""
    return STINTS.with_columns(
        pl.when(where).then(pl.col(c) * 2).otherwise(pl.col(c)).alias(c)
        for c in ("home_xg", "away_xg")
    )


def rate(
    stints: pl.DataFrame = STINTS,
    settings: rapm.Settings = AGED,
    roles: pl.DataFrame = ROLES,
    **kwargs: object,
) -> pl.DataFrame:
    ratings, _, _ = rapm.rate(
        fx.seasons_of(stints, THREE),
        GAMES,
        roles,
        VENUES,
        WANTED,
        settings,
        VERSION,
        players=fx.players(),
        league_seasons=fx.league_seasons(),
        **kwargs,  # type: ignore[arg-type]
    )
    return ratings.sort("game_id", "player_id", "component")


@cache
def base() -> pl.DataFrame:
    return rate()


def on(ratings: pl.DataFrame, night: object) -> pl.DataFrame:
    return ratings.filter(pl.col("game_date") == night)


def same(a: pl.DataFrame, b: pl.DataFrame) -> None:
    for column in ("mean", "prior", "sd", "hours"):
        np.testing.assert_allclose(
            a[column].fill_null(-1.0).to_numpy(),
            b[column].fill_null(-1.0).to_numpy(),
            rtol=1e-9,
            atol=1e-12,
        )


def test_aging_is_in_play() -> None:
    # Without this the tests below would pass for want of an aging shift.
    unaged = rate(settings=rapm.Settings(180.0, 2.0, 0.0))
    assert not np.allclose(on(unaged, OPENING)["mean"], on(base(), OPENING)["mean"])


def test_the_season_s_own_stints_never_move_its_opening_night() -> None:
    moved = rate(doubled(pl.col("season") == SEASON))
    same(on(moved, OPENING), on(base(), OPENING))


def test_tonights_and_later_stints_never_move_tonights_ratings() -> None:
    moved = rate(doubled(pl.col("game_date") >= NIGHT))
    same(moved, base())


def test_earlier_seasons_stints_do_move_the_opening_night() -> None:
    moved = rate(doubled(pl.col("season") == THREE[1]))
    assert not np.allclose(on(moved, OPENING)["mean"], on(base(), OPENING)["mean"])


def tuning_ratings(stints: pl.DataFrame = STINTS, roles: pl.DataFrame = ROLES) -> pl.DataFrame:
    """The ratings as the tuning fits them: 5v5 only, without spreads."""
    return rate(stints, roles=roles, kinds=(rapm.EV,), spread=False)


def feature(ratings: pl.DataFrame, lineups: pl.DataFrame = LINEUPS) -> pl.DataFrame:
    return rapm.expected_difference(ratings, lineups, GAMES)


TONIGHT = GAMES.filter(pl.col("game_date") == NIGHT)["game_id"].implode()


@cache
def base_x() -> pl.DataFrame:
    return feature(tuning_ratings())


def tonight_x(frame: pl.DataFrame) -> NDArray[np.float64]:
    return frame.filter(pl.col("game_id").is_in(TONIGHT)).sort("game_id")["x"].to_numpy()


def test_the_feature_covers_tonights_games() -> None:
    assert len(tonight_x(base_x())) == GAMES.filter(pl.col("game_date") == NIGHT).height
    assert np.all(tonight_x(base_x()) != 0.0)


def test_later_games_ratings_and_lineups_never_move_tonights_feature() -> None:
    later = pl.col("game_date") > NIGHT
    ratings = tuning_ratings().with_columns(
        mean=pl.when(later).then(pl.col("mean") + 5.0).otherwise(pl.col("mean"))
    )
    lineups = LINEUPS.with_columns(
        exp_5v5=pl.when(later).then(pl.col("exp_5v5") * 3).otherwise(pl.col("exp_5v5"))
    )
    np.testing.assert_allclose(tonight_x(feature(ratings, lineups)), tonight_x(base_x()))


def test_tonights_and_later_stints_and_roles_never_move_tonights_feature() -> None:
    from_tonight = GAMES.filter(pl.col("game_date") >= NIGHT)["game_id"].implode()
    swapped = pl.when(pl.col("role") == "D").then(pl.lit("F")).otherwise(pl.lit("D"))
    roles = ROLES.with_columns(
        role=pl.when(pl.col("game_id").is_in(from_tonight)).then(swapped).otherwise(pl.col("role"))
    )
    ratings = tuning_ratings(doubled(pl.col("game_date") >= NIGHT), roles)
    np.testing.assert_allclose(
        tonight_x(feature(ratings)), tonight_x(base_x()), rtol=1e-9, atol=1e-12
    )


def test_earlier_stints_do_move_it() -> None:
    ratings = tuning_ratings(doubled(pl.col("game_date") < NIGHT))
    assert not np.allclose(tonight_x(feature(ratings)), tonight_x(base_x()))
