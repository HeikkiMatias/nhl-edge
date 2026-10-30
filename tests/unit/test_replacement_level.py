"""scripts/replacement_level.py (#134, ADR 0051): its 5v5 xG difference with replacements rated 0
is B3's own, a level moves each side by its replacements' minutes, newcomers and the levels are
worked by hand, the fitted level recovers a known one, and the report renders."""

from dataclasses import replace

import b3_fixtures as b3f
import numpy as np
import polars as pl
import pytest
from replacement_fixtures import (
    CANDIDATES,
    DRESSED,
    G1,
    G2,
    GAMES,
    RATED,
    SEASON,
    game_rows,
    levels,
    rl,
)

from nhl_edge.game import b3

ZERO = {f"{p}_{r}": pl.lit(0.0) for p in ("h", "a") for r in rl.ROLES}


def test_the_5v5_difference_with_replacements_rated_0_is_b3_s() -> None:
    tables = b3f.league(seasons=(20162017, 20172018), games=40)
    spare = tables.lineup_replacements.with_columns(
        exp_5v5=pl.when(pl.col("game_id") % 3 == 0).then(30.0).otherwise(0.0)
    )
    tables = replace(tables, lineup_replacements=spare)
    goals = b3.team_goals(tables)
    home = goals.filter("is_home").select("game_id", home_xg="xg_5v5")
    away = goals.filter(~pl.col("is_home")).select("game_id", away_xg="xg_5v5")
    rated = rl.five_ratings(tables.player_ratings.with_columns(prior=pl.lit(0.0)))
    rows = rl.game_rows(tables.games, rl.team_minutes(tables.lineups, rated, spare))
    joined = rows.join(home, on="game_id").join(away, on="game_id")
    assert joined.height == home.height
    assert (joined["h_spare_F"] > 0).any()
    np.testing.assert_allclose(
        joined["x0"].to_numpy(), (joined["home_xg"] - joined["away_xg"]).to_numpy(), atol=1e-12
    )


def test_a_level_moves_each_side_by_its_replacements_minutes() -> None:
    rows = game_rows((SEASON,), games=5)
    home = rows.select(rl.rated_at(rows, ZERO | {"h_F": pl.lit(0.1)}) - pl.col("x0")).to_series()
    away = rows.select(rl.rated_at(rows, ZERO | {"a_D": pl.lit(0.1)}) - pl.col("x0")).to_series()
    expected = rows["hours"] * rl.ON_ICE * 0.1
    np.testing.assert_allclose(home, expected * rows["h_spare_F"] / rows["h_all_min"])
    np.testing.assert_allclose(away, -expected * rows["a_spare_D"] / rows["a_all_min"])


def test_newcomers_by_hand() -> None:
    new = rl.newcomers(DRESSED, CANDIDATES, RATED, GAMES).sort("game_id", "player_id")
    assert new.select("game_id", "player_id").rows() == [(G1, 1), (G2, 2), (G2, 3)]
    assert new["opener"].to_list() == [True, False, False]
    # 1: last season's rating; 2: his season prior, since his only earlier rating is as of the
    # game's start; 3: neither, so the reference skater.
    assert new["known"].to_list() == pytest.approx([0.10, -0.05, 0.0])
    assert new["prior"].to_list()[1:] == [-0.05, None]


def test_the_levels_by_hand() -> None:
    earlier = SEASON - 10_001
    seasons = pl.DataFrame(
        {
            "season": [earlier] * 4,
            "player_id": [1, 2, 3, 4],
            "games": [10, 5, 30, 8],
            "role": ["F", "F", "F", "D"],
            "end_level": [-0.10, 0.05, 0.20, -0.20],
        }
    ).with_columns(pl.col("season").cast(pl.Int32))
    new = pl.DataFrame(
        {
            "season": [earlier] * 4,
            "role": ["F", "F", "D", "D"],
            "opener": [False, True, False, True],
            "prior": [-0.02, -0.04, -0.03, None],
            "known": [-0.05, 0.01, -0.10, -0.06],
        }
    ).with_columns(pl.col("season").cast(pl.Int32))
    got = rl.season_levels(SEASON, seasons, new)
    # Fringe: the 20-games-or-fewer skaters' season-end ratings, weighted by games.
    assert got[rl.FRINGE]["F"] == pytest.approx((10 * -0.10 + 5 * 0.05) / 15)
    assert got[rl.FRINGE]["D"] == pytest.approx(-0.20)
    assert got[rl.PRIOR] == pytest.approx({"F": -0.03, "D": -0.03})
    assert got[f"{rl.NEWCOMER}_in"] == pytest.approx({"F": -0.05, "D": -0.10})
    assert got[f"{rl.NEWCOMER}_open"] == pytest.approx({"F": 0.01, "D": -0.06})
    # The first season has nothing earlier: every level is the reference skater's.
    assert rl.season_levels(earlier, seasons, new)[rl.FRINGE] == {"F": 0.0, "D": 0.0}


def test_the_fitted_levels_recover_known_ones() -> None:
    rows = game_rows((SEASON,), games=200)
    truth = {"h_F": 0.3, "a_F": 0.3, "h_D": -0.2, "a_D": -0.2}
    rows = rows.with_columns(r=2 * rl.rated_at(rows, {k: pl.lit(v) for k, v in truth.items()}))
    assert rl.fitted_levels(rows) == pytest.approx({"F": 0.3, "D": -0.2})


def test_the_report_renders_every_option_and_subset() -> None:
    seasons = (SEASON - 10_001, SEASON, SEASON + 10_001)
    rows = game_rows(seasons)
    by_season = {s: levels(-0.02) for s in seasons}
    scored = rl.score(rows, rows.select("season", "start_utc"), by_season, seasons[1:])
    new = rl.newcomers(DRESSED, CANDIDATES, RATED, GAMES)
    text = rl.report(scored, by_season, rl.shares(rows), new, "replacement-level-20261030-abc1234")
    for kind in rl.LEVELS:
        assert text.count(f"| {kind} |") == 4
    assert "Either team's first game of the season" in text
    # The hand frames' newcomers: one in a first game (rated last season), two in-season, of
    # whom neither was rated before his game.
    assert f"| {SEASON} | 2 | 0% | 1 | 100% |" in text
