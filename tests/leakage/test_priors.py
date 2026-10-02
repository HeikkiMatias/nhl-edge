"""Point-in-time rules for the priors (#102, ADR 0020, hard rule 1). A season's priors (its NHLe
factors, trait effects and age curves) read only lines, stints and ratings public before its
first as-of time: a future debutant never shapes them, a line public exactly then is not read,
and the season's own stints never move its trait effects."""

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest
import rapm_fixtures as fx

from nhl_edge.lineup import goalie_start as gs
from nhl_edge.ratings import priors as pr
from nhl_edge.ratings import rapm

LEAGUE = fx.league()
GAMES, STINTS, ROLES, VENUES = (LEAGUE[k] for k in ("games", "stints", "roles", "venues"))
SETTINGS = rapm.Settings(half_life_days=20.0, pull_hours=2.0)
SEASON = fx.SEASONS[1]
CUTOFF = gs.season_cutoff(GAMES, SEASON)
WANTED = rapm.targets(LEAGUE["lineups"], GAMES, [SEASON])
OPENING = WANTED.filter(pl.col("game_date") == WANTED["game_date"].min())
FUTURE = 7777


def run(
    stints: pl.DataFrame = STINTS,
    players: pl.DataFrame | None = None,
    lines: pl.DataFrame | None = None,
) -> tuple[pl.DataFrame, pr.PriorFit]:
    ratings, _, fits = rapm.rate(
        fx.seasons_of(stints),
        GAMES,
        ROLES,
        VENUES,
        OPENING,
        SETTINGS,
        "rapm-20261002-abc1234",
        players=fx.players() if players is None else players,
        league_seasons=fx.league_seasons() if lines is None else lines,
    )
    (fit,) = [f for f in fits if f.season == SEASON]
    return ratings.sort("game_id", "player_id", "component"), fit


BASE, BASE_FIT = run()


def same(a: tuple[pl.DataFrame, pr.PriorFit], b: tuple[pl.DataFrame, pr.PriorFit]) -> None:
    np.testing.assert_allclose(a[0]["prior"].to_numpy(), b[0]["prior"].to_numpy(), atol=1e-12)
    np.testing.assert_allclose(a[0]["mean"].to_numpy(), b[0]["mean"].to_numpy(), atol=1e-12)
    assert a[1].effects == b[1].effects
    assert a[1].factors.equals(b[1].factors)


def _debutant_lines(observed: pl.Expr) -> pl.DataFrame:
    """A future debutant's AHL season and NHL season, scoring far above the fixture's moves."""
    rows = [
        (FUTURE, 20102011, "AHL", 60, 120),
        (FUTURE, 20112012, "NHL", 60, 300),
    ]
    base = fx.league_seasons()
    extra = pl.DataFrame(
        rows, schema=["player_id", "season", "league", "games_played", "points"], orient="row"
    ).with_columns(
        pl.col("season").cast(pl.Int32),
        league_abbrev=pl.col("league"),
        game_type=pl.lit(2, pl.Int8),
        games_played=pl.col("games_played").cast(pl.Int16),
        goals=(pl.col("points") // 2).cast(pl.Int16),
        assists=(pl.col("points") - pl.col("points") // 2).cast(pl.Int16),
        observed_utc=observed,
    )
    return pl.concat([base, extra.select(base.columns)])


def test_a_future_debutant_never_shapes_a_season_s_priors() -> None:
    # Public only after the season starts, as player_league_seasons stamps a debutant's lines.
    later = pl.lit(CUTOFF + timedelta(days=30), dtype=pl.Datetime("us", "UTC"))
    moved = run(lines=_debutant_lines(later))
    same(moved, (BASE, BASE_FIT))


def test_a_line_public_at_the_cutoff_is_not_read() -> None:
    at = run(lines=_debutant_lines(pl.lit(CUTOFF, dtype=pl.Datetime("us", "UTC"))))
    same(at, (BASE, BASE_FIT))


def test_earlier_lines_do_shape_them() -> None:
    before = pl.lit(CUTOFF.replace(month=7, day=1), dtype=pl.Datetime("us", "UTC"))
    _, fit = run(lines=_debutant_lines(before))
    assert not fit.factors.equals(BASE_FIT.factors)


def test_a_future_debutant_s_traits_never_shape_the_effects() -> None:
    extra = pl.DataFrame(
        {"player_id": [FUTURE], "birth_date": [date(1999, 1, 1)], "draft_overall": [1]},
        schema={"player_id": pl.Int64, "birth_date": pl.Date, "draft_overall": pl.Int16},
    )
    moved = run(players=pl.concat([fx.players(), extra]))
    same(moved, (BASE, BASE_FIT))


def test_the_season_s_own_stints_never_move_its_effects() -> None:
    doubled = STINTS.with_columns(
        pl.when(pl.col("season") == SEASON).then(pl.col(c) * 2).otherwise(pl.col(c)).alias(c)
        for c in ("home_xg", "away_xg")
    )
    _, fit = run(stints=doubled)
    assert fit.effects == BASE_FIT.effects


def test_earlier_stints_do_move_them() -> None:
    doubled = STINTS.with_columns(
        pl.when(pl.col("season") < SEASON).then(pl.col(c) * 2).otherwise(pl.col(c)).alias(c)
        for c in ("home_xg", "away_xg")
    )
    _, fit = run(stints=doubled)
    assert fit.effects != BASE_FIT.effects


# Three seasons, so the third has age curves from the first two's season ends.
THREE = (*fx.SEASONS, 20132014)
LONG = fx.league(seasons=THREE)


def _curves(stints: pl.DataFrame) -> dict[str, pr.AgeCurve]:
    wanted = rapm.targets(LONG["lineups"], LONG["games"], [THREE[2]])
    opening = wanted.filter(pl.col("game_date") == wanted["game_date"].min())
    _, _, fits = rapm.rate(
        fx.seasons_of(stints, THREE),
        LONG["games"],
        LONG["roles"],
        LONG["venues"],
        opening,
        SETTINGS,
        "rapm-20261002-abc1234",
        players=fx.players(),
        league_seasons=fx.league_seasons(),
    )
    return dict(fits[-1].curves)


def test_a_season_s_age_curves_read_only_seasons_that_ended_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fixture's players have a few hours a season; let them all count.
    monkeypatch.setattr(pr, "AGE_HOURS", 0.5)
    monkeypatch.setattr(pr, "AGE_PP_HOURS", 0.1)
    base = _curves(LONG["stints"])
    assert base and all(c.pairs > 0 for c in base.values())
    season = THREE[2]
    own = LONG["stints"].with_columns(
        pl.when(pl.col("season") >= season).then(pl.col(c) * 2).otherwise(pl.col(c)).alias(c)
        for c in ("home_xg", "away_xg")
    )
    assert _curves(own) == base
    earlier = LONG["stints"].with_columns(
        pl.when(pl.col("season") == THREE[1]).then(pl.col(c) * 2).otherwise(pl.col(c)).alias(c)
        for c in ("home_xg", "away_xg")
    )
    assert _curves(earlier) != base


def test_the_season_start_fit_refuses_stints_public_after_the_season_starts() -> None:
    # The first season's last night, stamped public after the second season starts.
    first = STINTS.filter(pl.col("season") == fx.SEASONS[0])["game_date"].max()
    last_night = (pl.col("season") == fx.SEASONS[0]) & (pl.col("game_date") == first)
    late = STINTS.with_columns(
        observed_utc=pl.when(last_night)
        .then(pl.lit(CUTOFF + timedelta(days=1), dtype=pl.Datetime("us", "UTC")))
        .otherwise(pl.col("observed_utc"))
    )
    assert (late.filter(last_night)["observed_utc"] > CUTOFF).all()
    with pytest.raises(ValueError, match="after"):
        run(stints=late)
