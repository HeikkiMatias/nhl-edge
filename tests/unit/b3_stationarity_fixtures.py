"""Helpers for scripts/b3_stationarity.py's tests (#378): the script as a module, and made-up
games as B3 trains on them, each season with its own Δĝ coefficient and intercept."""

import importlib.util
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType

import numpy as np
import polars as pl

from nhl_edge.game import b3


def load() -> ModuleType:
    """scripts/b3_stationarity.py as a module."""
    path = Path(__file__).parents[2] / "scripts" / "b3_stationarity.py"
    spec = importlib.util.spec_from_file_location("b3_stationarity", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def games(
    truths: Mapping[int, tuple[float, float]], per_season: int = 1200, seed: int = 5
) -> pl.DataFrame:
    """SYNTHETIC rows: per season, games over 25 weeks from October with B3's inputs (Δĝ about
    0.4 goals wide, the schedule terms small), h_s as the offset, and a home win drawn from the
    season's (coefficient per goal, intercept)."""
    rng = np.random.default_rng(seed)
    frames = []
    for i, (season, (coefficient, intercept)) in enumerate(sorted(truths.items())):
        start = date(season // 10000, 10, 10)
        x = {name: rng.normal(0, 0.1, per_season) for name in b3.INPUTS}
        x["delta_g_hat"] = rng.normal(0, 0.4, per_season)
        offset = np.full(per_season, 0.15)
        eta = intercept + offset + coefficient * x["delta_g_hat"]
        won = (rng.random(per_season) < 1 / (1 + np.exp(-eta))).astype(np.int8)
        frames.append(
            pl.DataFrame(
                {
                    "game_id": np.arange(per_season) + season * 10 + i,
                    "season": [season] * per_season,
                    "game_date": [
                        start + timedelta(days=int(d)) for d in rng.integers(0, 175, per_season)
                    ],
                    **x,
                    "offset": offset,
                    "home_win": won,
                    "known_utc": [None] * per_season,
                }
            ).with_columns(
                pl.col("season").cast(pl.Int32),
                known_utc=pl.col("game_date").cast(pl.Datetime("us", "UTC")),
            )
        )
    return pl.concat(frames)
