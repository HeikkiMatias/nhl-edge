"""RAPM's tuning (#103, ADR 0011): the aging shift, the grid, the feature, and nhl rapm --tune."""

from typing import Any

import numpy as np
import polars as pl
import pytest
import rapm_fixtures as fx
import scipy.sparse as sp

from nhl_edge.ratings import priors as pr
from nhl_edge.ratings import rapm


def test_aging_shifts_the_evidence_as_if_the_responses_had_moved() -> None:
    rng = np.random.default_rng(4)
    n, p = 300, 5
    x = rng.normal(size=(n, p))
    hours = rng.uniform(0.01, 0.03, n)
    y = x @ rng.normal(size=p) + rng.normal(0, 0.1, n)
    days = np.repeat(np.arange(6.0), n // 6)
    pull = np.array([0.0, 0.5, 0.5, 0.5, 0.5])
    shift = np.array([0.0, 0.1, -0.2, 0.05, 0.0])
    aged = rapm.Normal(4.0)
    aged.add(sp.csr_matrix(x), hours, y, days)
    aged.age(shift)
    moved = rapm.Normal(4.0)
    moved.add(sp.csr_matrix(x), hours, y + x @ shift, days)
    a = aged.solve(pull, np.arange(1, p))
    b = moved.solve(pull, np.arange(1, p))
    np.testing.assert_allclose(a.beta, b.beta, rtol=1e-9, atol=1e-12)
    assert a.sigma == pytest.approx(b.sigma, rel=1e-9)


def test_the_grid_has_the_owner_s_36_candidates() -> None:
    assert len(rapm.GRID) == 36
    assert {s.pull_hours for s in rapm.GRID} == {10.0, 20.0, 40.0, 80.0}
    assert {s.half_life_days for s in rapm.GRID} == {90.0, 180.0, 360.0}
    assert {s.aging for s in rapm.GRID} == {0.0, 0.5, 1.0}
    steadiest = max(rapm.GRID, key=rapm.steadiness)
    assert steadiest == rapm.Settings(360.0, 80.0, 1.0)
    # More pull beats a longer memory, which beats fuller aging.
    assert rapm.steadiness(rapm.Settings(90.0, 40.0, 0.0)) > rapm.steadiness(
        rapm.Settings(360.0, 20.0, 1.0)
    )


def test_the_feature_is_the_home_margin_of_minutes_times_ratings() -> None:
    games = pl.DataFrame({"game_id": [1], "home": ["BOS"], "away": ["TOR"]})
    lineups = pl.DataFrame(
        {
            "game_id": [1, 1, 1, 1],
            "team": ["BOS", "BOS", "TOR", "TOR"],
            "player_id": [10, 11, 20, 21],
            "role": ["F", "D", "F", "G"],
            "exp_5v5": [12.0, 18.0, 15.0, None],
        }
    )
    ratings = pl.DataFrame(
        {
            "game_id": [1] * 6,
            "player_id": [10, 10, 11, 11, 20, 20],
            "component": ["ev_off", "ev_def", "ev_off", "ev_def", "ev_off", "pp"],
            "mean": [0.3, 0.1, -0.1, 0.2, 0.5, 9.0],
        }
    )
    got = rapm.expected_difference(ratings, lineups, games)
    home = 12 / 60 * 0.4 + 18 / 60 * 0.1
    away = 15 / 60 * 0.5
    assert got["x"][0] == pytest.approx(home - away)


def test_a_team_without_projected_skaters_counts_as_replacement_level() -> None:
    # Game 2: the away team, an expansion team's first game, has no projected skaters. Game 3
    # has no lineup on either side and is left out.
    games = pl.DataFrame({"game_id": [2, 3], "home": ["DAL", "BOS"], "away": ["VGK", "TOR"]})
    lineups = pl.DataFrame(
        {"game_id": [2], "team": ["DAL"], "player_id": [10], "role": ["F"], "exp_5v5": [12.0]}
    )
    ratings = pl.DataFrame(
        {
            "game_id": [2, 2],
            "player_id": [10, 10],
            "component": ["ev_off", "ev_def"],
            "mean": [0.3, 0.1],
        }
    )
    got = rapm.expected_difference(ratings, lineups, games)
    assert got["game_id"].to_list() == [2]
    assert got["x"][0] == pytest.approx(12 / 60 * 0.4)


THREE = (*fx.SEASONS, 20132014)
LONG = fx.league(seasons=THREE)


def _third_season(settings: rapm.Settings) -> pl.DataFrame:
    wanted = rapm.targets(LONG["lineups"], LONG["games"], [THREE[2]])
    ratings, _, _ = rapm.rate(
        fx.seasons_of(LONG["stints"], THREE),
        LONG["games"],
        LONG["roles"],
        LONG["venues"],
        wanted,
        settings,
        "rapm-20261003-abc1234",
        players=fx.players(),
        league_seasons=fx.league_seasons(),
        kinds=(rapm.EV,),
        spread=False,
    )
    return ratings.sort("game_id", "player_id", "component")


def test_aging_moves_the_ratings_once_age_curves_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr, "AGE_HOURS", 0.5)
    without = _third_season(rapm.Settings(180.0, 2.0, 0.0))
    with_aging = _third_season(rapm.Settings(180.0, 2.0, 1.0))
    assert not np.allclose(without["mean"].to_numpy(), with_aging["mean"].to_numpy())
    assert set(without["component"].unique()) == {"ev_off", "ev_def"}
    # Without spreads, a rated player's sd stays null.
    assert without.filter(pl.col("hours") > 0)["sd"].is_null().all()


def tune_lake(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from nhl_edge import reference
    from nhl_edge.lake.tables import Lake

    frames: dict[str, Any] = fx.league()
    frames["actual_lineups"] = frames.pop("roles")
    frames["shift_coverage"] = (
        frames["stints"].select("game_id", "season").unique().with_columns(complete=pl.lit(True))
    )
    frames["players"] = fx.players().with_columns(name=pl.lit("A Skater"))
    frames["player_league_seasons"] = fx.league_seasons()
    frames["lineups"] = frames["lineups"].with_columns(exp_5v5=pl.lit(15.0))
    venues = frames.pop("venues")
    monkeypatch.setattr(reference, "load_venues", lambda: venues)

    def read(self: Lake, table: str, seasons: Any = None) -> pl.DataFrame:
        frame = frames[table]
        return frame if seasons is None else frame.filter(pl.col("season").is_in(list(seasons)))

    monkeypatch.setattr(Lake, "read", read)
    monkeypatch.setattr(rapm, "TUNING_SEASONS", (fx.SEASONS[1],))
    monkeypatch.setattr(
        rapm, "GRID", (rapm.Settings(180.0, 0.5, 0.0), rapm.Settings(180.0, 20.0, 0.0))
    )
    return frames


def test_tune_logs_every_candidate_and_writes_no_table(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app

    tune_lake(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["rapm", "--tune"])
    assert result.exit_code == 0, result.output
    (path,) = (tmp_path / "reports" / "tuning").glob("rapm-*.md")
    text = path.read_text()
    assert "# Tuning: RAPM" in text and text.count("| half-life 180 game days") == 2
    assert "chosen" in result.output
    assert not (tmp_path / "data").exists()


def test_tune_refuses_seasons_or_r2(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app

    tune_lake(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["rapm", "--tune", "--seasons", "20112012"])
    assert result.exit_code == 2


def test_the_frozen_settings_are_a_grid_point() -> None:
    # The owner's choice on 2026-10-03 (ADR 0011): the rule's pull and memory without aging.
    assert rapm.TUNED in rapm.GRID
    assert rapm.Settings(half_life_days=360.0, pull_hours=80.0, aging=0.0) == rapm.TUNED
