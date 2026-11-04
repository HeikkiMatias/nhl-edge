"""Helpers for scripts/ot_winner.py's tests (#379): the script as a module, and made-up ties
whose overtime winner follows the opener's log-odds by a chosen slope."""

import importlib.util
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType

import numpy as np
import polars as pl


def load() -> ModuleType:
    """scripts/ot_winner.py as a module."""
    path = Path(__file__).parents[2] / "scripts" / "ot_winner.py"
    spec = importlib.util.spec_from_file_location("ot_winner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SEASONS = (20152016, 20162017, 20172018, 20182019, 20192020, 20202021, 20212022)


def league(
    slope: float, per_season: int = 1200, seed: int = 4
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, dict[int, float]]:
    """SYNTHETIC: per season, games over 25 weeks from October with an opener o and Δĝ; about a
    quarter go to overtime, whose home winner follows sigmoid(0.1 + slope·o). Returns the ties,
    the inputs (game_id, o, delta_g_hat), the games (game_id, season) and the draw rates."""
    rng = np.random.default_rng(seed)
    ties, inputs, games = [], [], []
    for i, season in enumerate(SEASONS):
        ids = np.arange(per_season) + (i + 1) * 100_000
        o = rng.normal(0.2, 0.4, per_season)
        start = date(season // 10000, 10, 10)
        dates = [start + timedelta(days=int(d)) for d in rng.integers(0, 175, per_season)]
        tied = rng.random(per_season) < 0.25
        p = 1 / (1 + np.exp(-(0.1 + slope * o)))
        won = (rng.random(per_season) < p).astype(np.int8)
        ties.append(
            pl.DataFrame(
                {"game_id": ids, "season": season, "game_date": dates, "home_won": won}
            ).filter(pl.Series(tied))
        )
        inputs.append(pl.DataFrame({"game_id": ids, "o": o, "delta_g_hat": o / 1.4}))
        games.append(pl.DataFrame({"game_id": ids, "season": [season] * per_season}))
    rates = {season: 0.25 for season in SEASONS}
    tied = pl.concat(ties).with_columns(
        pl.col("season").cast(pl.Int32), pl.col("home_won").cast(pl.Int8)
    )
    games_frame = pl.concat(games).with_columns(pl.col("season").cast(pl.Int32))
    return tied, pl.concat(inputs), games_frame, rates
