"""Helpers for scripts/close_forecast_history.py's tests (#369): the script as a module, and the
yardstick's made-up rows with B3's four parts, the home edge and B3's whole difference."""

import importlib.util
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
