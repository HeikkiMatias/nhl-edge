"""Helpers for scripts/close_forecast_history.py's tests (#369): the script as a module, and the
yardstick's made-up rows with B3's four parts, the home edge and B3's whole difference."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import polars as pl

from nhl_edge.game import b3


def load() -> ModuleType:
    """scripts/close_forecast_history.py as a module."""
    path = Path(__file__).parents[2] / "scripts" / "close_forecast_history.py"
    spec = importlib.util.spec_from_file_location("close_forecast_history", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def with_parts(frame: pl.DataFrame, seed: int = 3) -> pl.DataFrame:
    """yf.moves' rows with B3's four parts and the home edge: the move follows the 5v5 part (x)."""
    rng = np.random.default_rng(seed)
    n = frame.height
    return frame.with_columns(
        five_diff=pl.col("x"),
        special_diff=pl.col("z"),
        finishing_diff=pl.col("w"),
        goalie_diff=pl.Series(rng.normal(0, 0.1, n)),
        h_s=pl.Series(rng.normal(0.1, 0.02, n)),
    ).with_columns(delta_g_hat=sum(pl.col(part) for part in b3.SPLIT))


def load_density() -> ModuleType:
    """scripts/close_forecast_density_history.py as a module (ADR 0056). It imports version 1's
    script by name, as it does when run from scripts/."""
    scripts = str(Path(__file__).parents[2] / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    path = Path(scripts) / "close_forecast_density_history.py"
    spec = importlib.util.spec_from_file_location("close_forecast_density_history", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def with_density(frame: pl.DataFrame, seed: int = 4) -> pl.DataFrame:
    """with_parts' rows with the five density columns (ADR 0056): mismatch moves the close, the
    others are noise."""
    rng = np.random.default_rng(seed)
    n = frame.height
    mismatch = rng.choice([-1.0, 0.0, 0.0, 1.0], n)
    return (
        with_parts(frame)
        .with_columns(
            dense3_diff=pl.Series(rng.integers(-2, 3, n).astype(float)),
            dense6_diff=pl.Series(rng.integers(-3, 4, n).astype(float)),
            mismatch=pl.Series(mismatch),
            home_return=pl.Series(rng.integers(0, 4, n).astype(float)),
            away_trip=pl.Series(rng.integers(0, 4, n).astype(float)),
        )
        .with_columns(y=pl.col("y") + 0.08 * pl.col("mismatch"))
    )
