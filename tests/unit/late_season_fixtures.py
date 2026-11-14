"""Helpers for scripts/late_season_stakes.py's tests (#422): the script as a module and SYNTHETIC
games, standings rows, flags, lineups and boxscores."""

import importlib.util
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import ModuleType

import polars as pl

UTC_TYPE = pl.Datetime("us", "UTC")


def load() -> ModuleType:
    """scripts/late_season_stakes.py as a module."""
    path = Path(__file__).parents[2] / "scripts" / "late_season_stakes.py"
    spec = importlib.util.spec_from_file_location("late_season_stakes", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def morning_after(day: date) -> datetime:
    """10:00 UTC the morning after day, when its results and standings count as public."""
    return datetime.combine(day + timedelta(days=1), time(10), UTC)


def games(rows: Sequence[tuple[int, date, str, str, int, int, str]], season: int) -> pl.DataFrame:
    """SYNTHETIC games: (game_id, date, home, away, home_score, away_score, decided_in), each
    starting at 23:00 UTC on its date."""
    return pl.DataFrame(
        {
            "game_id": [r[0] for r in rows],
            "season": [season] * len(rows),
            "game_date": [r[1] for r in rows],
            "start_utc": [datetime.combine(r[1], time(23), UTC) for r in rows],
            "home": [r[2] for r in rows],
            "away": [r[3] for r in rows],
            "home_score": [r[4] for r in rows],
            "away_score": [r[5] for r in rows],
            "decided_in": [r[6] for r in rows],
        },
        schema_overrides={"season": pl.Int32, "start_utc": UTC_TYPE},
    )


def standings(
    rows: Sequence[tuple[str, str, str, int, int]],
    day: date,
    season: int,
    clinch: dict[str, str] | None = None,
) -> pl.DataFrame:
    """SYNTHETIC standings of one date: (team, conference, division, games_played, points), with
    the clinch flags given by team."""
    flags = clinch or {}
    return pl.DataFrame(
        {
            "season": [season] * len(rows),
            "standings_date": [day] * len(rows),
            "team": [r[0] for r in rows],
            "conference": [r[1] for r in rows],
            "division": [r[2] for r in rows],
            "games_played": [r[3] for r in rows],
            "points": [r[4] for r in rows],
            "clinch_indicator": [flags.get(r[0]) for r in rows],
            "observed_utc": [morning_after(day)] * len(rows),
        },
        schema_overrides={
            "season": pl.Int32,
            "games_played": pl.Int16,
            "points": pl.Int16,
            "clinch_indicator": pl.String,
            "observed_utc": UTC_TYPE,
        },
    )


def flagged(rows: Sequence[tuple[str, date, bool, bool]]) -> pl.DataFrame:
    """SYNTHETIC flags as flags() gives them: (team, standings_date, clinched, eliminated), each
    observed the morning after its date."""
    return pl.DataFrame(
        {
            "team": [r[0] for r in rows],
            "standings_date": [r[1] for r in rows],
            "observed_utc": [morning_after(r[1]) for r in rows],
            "clinched": [r[2] for r in rows],
            "eliminated": [r[3] for r in rows],
        },
        schema_overrides={"observed_utc": UTC_TYPE},
    )


def lineups(
    rows: Sequence[tuple[int, str, int, str, float | None, float | None, float]],
    observed_utc: datetime,
) -> pl.DataFrame:
    """SYNTHETIC projected lineups: (game_id, team, player_id, role, p_available, p_start,
    expected minutes in all), the minutes put at 5v5."""
    return pl.DataFrame(
        {
            "game_id": [r[0] for r in rows],
            "team": [r[1] for r in rows],
            "player_id": [r[2] for r in rows],
            "role": [r[3] for r in rows],
            "p_available": [r[4] for r in rows],
            "p_start": [r[5] for r in rows],
            "exp_5v5": [r[6] if r[3] != "G" else None for r in rows],
            "exp_pp": [0.0 if r[3] != "G" else None for r in rows],
            "exp_pk": [0.0 if r[3] != "G" else None for r in rows],
            "observed_utc": [observed_utc] * len(rows),
        },
        schema_overrides={
            "p_available": pl.Float64,
            "p_start": pl.Float64,
            "exp_5v5": pl.Float64,
            "exp_pp": pl.Float64,
            "exp_pk": pl.Float64,
            "observed_utc": UTC_TYPE,
        },
    )


def actual(rows: Sequence[tuple[int, str, int, bool]]) -> pl.DataFrame:
    """SYNTHETIC boxscore lineups: (game_id, team, player_id, starting_goalie)."""
    return pl.DataFrame(
        {
            "game_id": [r[0] for r in rows],
            "team": [r[1] for r in rows],
            "player_id": [r[2] for r in rows],
            "starting_goalie": [r[3] for r in rows],
        }
    )
