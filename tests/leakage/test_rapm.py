"""Point-in-time rules for RAPM (#101, ADR 0019, hard rule 1). A game's ratings and its fit's terms
read only stints public before its as-of time: its own night's stints and later ones never move
them, and a stint public exactly at the as-of time counts as not yet public."""

import numpy as np
import polars as pl
import rapm_fixtures as fx

from nhl_edge.lake.schemas import PlayerRatings, RapmTerms, dtypes
from nhl_edge.ratings import rapm

LEAGUE = fx.league()
GAMES, STINTS, ROLES, VENUES = (LEAGUE[k] for k in ("games", "stints", "roles", "venues"))
VERSION = "rapm-20261002-abc1234"
SETTINGS = rapm.Settings(half_life_days=20.0, pull_hours=2.0)
SEASON = fx.SEASONS[1]
NIGHT = GAMES.filter(pl.col("season") == SEASON)["game_date"].unique().sort()[10]
WANTED = rapm.targets(LEAGUE["lineups"], GAMES, [SEASON]).filter(pl.col("game_date") == NIGHT)
OUTPUTS = ("mean", "sd", "hours")
RATING_COLUMNS = [
    "game_id",
    "season",
    "game_date",
    "team",
    "player_id",
    "role",
    "component",
    "mean",
    "sd",
    "hours",
    "known_utc",
    "half_life_days",
    "pull_hours",
    "as_of_utc",
    "train_cutoff",
    "artifact_version",
    "observed_utc",
]


def tonight(stints: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    ratings, terms = rapm.rate(
        fx.seasons_of(stints), GAMES, ROLES, VENUES, WANTED, SETTINGS, VERSION
    )
    return ratings.sort("game_id", "player_id", "component"), terms.sort("model", "term")


def doubled(where: pl.Expr) -> pl.DataFrame:
    """STINTS with the xG of those where holds doubled."""
    return STINTS.with_columns(
        pl.when(where).then(pl.col(c) * 2).otherwise(pl.col(c)).alias(c)
        for c in ("home_xg", "away_xg")
    )


def same(a: pl.DataFrame, b: pl.DataFrame, columns: tuple[str, ...]) -> None:
    for column in columns:
        np.testing.assert_allclose(
            a[column].fill_null(-1.0).to_numpy(),
            b[column].fill_null(-1.0).to_numpy(),
            rtol=1e-9,
            atol=1e-12,
        )


BASE, BASE_TERMS = tonight(STINTS)


def test_tonights_and_later_stints_never_move_tonights_ratings() -> None:
    moved, terms = tonight(doubled(pl.col("game_date") >= NIGHT))
    same(moved, BASE, OUTPUTS)
    same(terms, BASE_TERMS, ("value", "hours"))


def test_earlier_stints_do_move_them() -> None:
    moved, _ = tonight(doubled(pl.col("game_date") < NIGHT))
    assert not np.allclose(moved["mean"].to_numpy(), BASE["mean"].to_numpy())


def test_a_stint_public_at_the_as_of_time_is_not_read() -> None:
    # The night before's stints, stamped public exactly at tonight's as-of time, count as not yet
    # public: the same as leaving them out.
    before = GAMES.filter(pl.col("game_date") < NIGHT)["game_date"].max()
    as_of = WANTED["as_of_utc"].min()
    late = pl.col("game_date") == before
    stamped = STINTS.with_columns(
        observed_utc=pl.when(late).then(pl.lit(as_of)).otherwise(pl.col("observed_utc"))
    )
    a, a_terms = tonight(stamped)
    b, b_terms = tonight(STINTS.filter(~late))
    same(a, b, OUTPUTS)
    same(a_terms, b_terms, ("value", "hours"))
    assert (a["known_utc"] < a["as_of_utc"]).all()


def test_every_row_was_read_before_its_as_of_time() -> None:
    assert (BASE["known_utc"] < BASE["as_of_utc"]).all()
    assert (BASE["observed_utc"] >= BASE["as_of_utc"]).all()
    assert (BASE_TERMS["known_utc"] < BASE_TERMS["as_of_utc"]).all()
    assert (BASE_TERMS["observed_utc"] >= BASE_TERMS["as_of_utc"]).all()


def test_the_rating_column_set_is_locked() -> None:
    assert list(dtypes(PlayerRatings)) == RATING_COLUMNS
    assert "known_utc" in dtypes(RapmTerms) and "as_of_utc" in dtypes(RapmTerms)
