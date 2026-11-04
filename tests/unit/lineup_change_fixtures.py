"""Helpers for scripts/lineup_change.py's tests (#370): the script as a module, and SYNTHETIC
tables for one game, BOS at home to NYR, with each team's previous game."""

import importlib.util
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import ModuleType

import polars as pl

D = date(2013, 11, 1)
SEASON = 20132014
TONIGHT = 3
PREDICTION = datetime.combine(D + timedelta(days=2), time(14, 0, 1), UTC)
UTC_TYPE = pl.Datetime("us", "UTC")


def load() -> ModuleType:
    """scripts/lineup_change.py as a module."""
    path = Path(__file__).parents[2] / "scripts" / "lineup_change.py"
    spec = importlib.util.spec_from_file_location("lineup_change", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def public(day: date) -> datetime:
    """A game's boxscore is public at 10:00 UTC the morning after (ADR 0003)."""
    return datetime.combine(day + timedelta(days=1), time(10), UTC)


def games() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "game_id": [1, 2, TONIGHT],
            "season": [SEASON] * 3,
            "game_date": [D, D + timedelta(days=1), D + timedelta(days=2)],
            "home": ["BOS", "NYR", "BOS"],
            "away": ["PHI", "TOR", "NYR"],
        }
    ).with_columns(pl.col("season").cast(pl.Int32))


def moments() -> pl.DataFrame:
    return pl.DataFrame({"game_id": [TONIGHT], "prediction_utc": [PREDICTION]}).with_columns(
        pl.col("prediction_utc").cast(UTC_TYPE)
    )


def boxscores(rows: list[tuple[int, date, str, int, str, int]] | None = None) -> pl.DataFrame:
    """Who dressed: (game_id, date, team, player_id, role, toi_s), public the morning after."""
    rows = rows or [
        (1, D, "BOS", 101, "F", 1000),
        (1, D, "BOS", 102, "D", 1000),
        (1, D, "BOS", 190, "G", 3600),
        (2, D + timedelta(days=1), "NYR", 201, "F", 1000),
        (2, D + timedelta(days=1), "NYR", 202, "D", 1000),
    ]
    return pl.DataFrame(
        {
            "game_id": [r[0] for r in rows],
            "season": [SEASON] * len(rows),
            "team": [r[2] for r in rows],
            "player_id": [r[3] for r in rows],
            "role": [r[4] for r in rows],
            "toi_s": [r[5] for r in rows],
            "observed_utc": [public(r[1]) for r in rows],
        }
    ).with_columns(pl.col("season").cast(pl.Int32), pl.col("observed_utc").cast(UTC_TYPE))


def projected(rows: list[tuple[str, int, str, float]] | None = None) -> pl.DataFrame:
    """Tonight's projection: (team, player_id, role, exp_5v5), observed at 10:00 ET."""
    rows = rows or [
        ("BOS", 101, "F", 10.0),
        ("BOS", 105, "D", 10.0),
        ("NYR", 201, "F", 10.0),
        ("NYR", 202, "D", 10.0),
    ]
    return pl.DataFrame(
        {
            "game_id": [TONIGHT] * len(rows),
            "team": [r[0] for r in rows],
            "player_id": [r[1] for r in rows],
            "role": [r[2] for r in rows],
            "exp_5v5": [r[3] for r in rows],
            "observed_utc": [PREDICTION - timedelta(seconds=1)] * len(rows),
        }
    ).with_columns(pl.col("observed_utc").cast(UTC_TYPE))


def rated(rows: list[tuple[int, int, float, datetime]] | None = None) -> pl.DataFrame:
    """Ratings: (game_id, player_id, ev_off + ev_def, observed_utc), split evenly over the two
    components, with a power-play row that never counts. 105 has none: a replacement."""
    before = PREDICTION - timedelta(hours=1)
    rows = rows or [
        (TONIGHT, 101, 0.2, before),
        (1, 102, 0.4, public(D)),
        (TONIGHT, 201, 0.1, before),
        (TONIGHT, 202, 0.1, before),
    ]
    out = []
    for game_id, player, value, seen in rows:
        for component, mean in (("ev_off", value / 2), ("ev_def", value / 2), ("pp", 9.0)):
            out.append(
                {
                    "game_id": game_id,
                    "player_id": player,
                    "component": component,
                    "mean": mean,
                    "observed_utc": seen,
                }
            )
    return pl.DataFrame(out).with_columns(pl.col("observed_utc").cast(UTC_TYPE))


def feature(**tables: pl.DataFrame) -> float:
    """lineup_change for tonight's game from the default tables, any of them replaced."""
    lc = load()
    found = lc.lineup_change(
        tables.get("games", games()),
        tables.get("lineups", projected()),
        tables.get("actual_lineups", boxscores()),
        tables.get("player_ratings", rated()),
        moments(),
    )
    (value,) = found["lineup_change"].to_list()
    return float(value)
