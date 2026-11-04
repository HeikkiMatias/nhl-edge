"""Helpers for scripts/targeted_features.py's tests (#370): the script as a module, and a made-up
schedule of played games."""

import importlib.util
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path
from types import ModuleType

import polars as pl


def load() -> ModuleType:
    """scripts/targeted_features.py as a module."""
    path = Path(__file__).parents[2] / "scripts" / "targeted_features.py"
    spec = importlib.util.spec_from_file_location("targeted_features", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def schedule(rows: Sequence[tuple[int, date, str, str]], season: int = 20132014) -> pl.DataFrame:
    """SYNTHETIC played games: (game_id, date, home, away), each starting at 00:00 UTC the next
    day (19:00 ET)."""
    return pl.DataFrame(
        {
            "game_id": [g for g, _, _, _ in rows],
            "season": [season] * len(rows),
            "game_date": [d for _, d, _, _ in rows],
            "start_utc": [datetime.combine(d, time(23, 0), tzinfo=UTC) for _, d, _, _ in rows],
            "home": [h for _, _, h, _ in rows],
            "away": [a for _, _, _, a in rows],
        },
        schema={
            "game_id": pl.Int64,
            "season": pl.Int32,
            "game_date": pl.Date,
            "start_utc": pl.Datetime("us", "UTC"),
            "home": pl.String,
            "away": pl.String,
        },
    )
