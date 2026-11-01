"""Hand-built games for scripts/upsets.py's tests (#366): BOS on a four-game winning run, all
but the first away, and TOR on a four-game losing run, meeting in game 9."""

import importlib.util
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import ModuleType

import polars as pl

UTC_TYPE = pl.Datetime("us", "UTC")


def load() -> ModuleType:
    """scripts/upsets.py as a module."""
    path = Path(__file__).parents[2] / "scripts" / "upsets.py"
    spec = importlib.util.spec_from_file_location("upsets", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def games(rows: list[tuple[int, int, str, str, int, int]]) -> pl.DataFrame:
    """(game_id, day of October 2013, home, away, home score, away score): each result public at
    10:00 UTC the next morning."""
    days = [date(2013, 10, r[1]) for r in rows]
    return pl.DataFrame(
        {
            "game_id": [r[0] for r in rows],
            "season": [20132014] * len(rows),
            "start_utc": [datetime.combine(d, time(23), UTC) for d in days],
            "home": [r[2] for r in rows],
            "away": [r[3] for r in rows],
            "home_score": [r[4] for r in rows],
            "away_score": [r[5] for r in rows],
            "observed_utc": [datetime.combine(d + timedelta(days=1), time(10), UTC) for d in days],
        }
    ).with_columns(
        pl.col("season").cast(pl.Int32),
        pl.col("home_score", "away_score").cast(pl.Int16),
        pl.col("start_utc", "observed_utc").cast(UTC_TYPE),
    )


# BOS wins four in a row, the last by 4, all but the first away; TOR loses four in a row.
GAMES = games(
    [
        (1, 1, "BOS", "MTL", 3, 2),
        (2, 3, "NYR", "BOS", 1, 2),
        (3, 5, "CHI", "BOS", 0, 1),
        (4, 7, "DET", "BOS", 1, 5),
        (5, 2, "TOR", "MTL", 1, 4),
        (6, 4, "TOR", "NYR", 0, 2),
        (7, 6, "CHI", "TOR", 3, 1),
        (8, 8, "DET", "TOR", 2, 0),
        (9, 9, "TOR", "BOS", 0, 0),
    ]
)


def frame(p_open: float = 0.30, p_close: float = 0.30) -> pl.DataFrame:
    """The yardstick's row for game 9, TOR at home against BOS, its opener at 14:00 UTC."""
    return pl.DataFrame(
        {
            "game_id": [9],
            "season": [20132014],
            "game_date": [date(2013, 10, 9)],
            "prediction_utc": [datetime(2013, 10, 9, 14, tzinfo=UTC)],
            "p_open": [p_open],
            "p_close": [p_close],
            "home_win": [0],
        }
    ).with_columns(
        pl.col("season").cast(pl.Int32),
        pl.col("home_win").cast(pl.Int8),
        pl.col("prediction_utc").cast(UTC_TYPE),
    )
