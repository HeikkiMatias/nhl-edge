"""Synthetic blend rows (backtest.blend.rows' columns) for the market blend's tests (#140): games
of several seasons whose results follow a known blend of the market and a model."""

from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import polars as pl

UTC_TYPE = pl.Datetime("us", "UTC")
WEIGHTS = {"a": 0.05, "b_m": 0.9, "b_x": 0.4, "b_u": -0.15}


def season_rows(
    season: int, games: int, seed: int, experiment: str = "E1", weights: dict[str, float] = WEIGHTS
) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    year = season // 10_000
    days = [date(year, 10, 10) + timedelta(days=i // 6) for i in range(games)]
    starts = [datetime.combine(d, time(23), UTC) for d in days]
    x_mkt = rng.normal(0.15, 0.5, games)
    x_model = 0.6 * x_mkt + rng.normal(0, 0.3, games)
    parts = rng.normal(size=(games, 3)) * [0.08, 0.5, 0.06] + [0.3, 1.6, 0.18]
    u = ((parts - [0.3, 1.6, 0.18]) / [0.08, 0.5, 0.06]).mean(axis=1)
    eta = weights["a"] + weights["b_m"] * x_mkt + (weights["b_x"] + weights["b_u"] * u) * x_model
    y = (rng.random(games) < 1 / (1 + np.exp(-eta))).astype(np.int8)
    fit_cutoff = datetime(year, 4, 15, 10, tzinfo=UTC)
    return pl.DataFrame(
        {
            "experiment": [experiment] * games,
            "season": [season] * games,
            "game_id": [year * 1_000_000 + 20_000 + i + 1 for i in range(games)],
            "game_date": days,
            "prediction_utc": starts,
            "home_win": y,
            "p_mkt": 1 / (1 + np.exp(-x_mkt)),
            "p_b2": 1 / (1 + np.exp(-(0.8 * x_model))),
            "p_b2_cutoff": [fit_cutoff] * games,
            "p_b3": 1 / (1 + np.exp(-x_model)),
            "p_b3_cutoff": [fit_cutoff] * games,
            "goalie_doubt": parts[:, 0],
            "availability_doubt": parts[:, 1],
            "rookie_share": parts[:, 2],
            "parts_utc": [s - timedelta(hours=8) for s in starts],
            "parts_cutoff": [fit_cutoff] * games,
            "result_utc": [datetime.combine(d + timedelta(days=1), time(10), UTC) for d in days],
        },
        schema_overrides={
            "season": pl.Int32,
            "home_win": pl.Int8,
            "prediction_utc": UTC_TYPE,
            "p_b2_cutoff": UTC_TYPE,
            "p_b3_cutoff": UTC_TYPE,
            "parts_utc": UTC_TYPE,
            "parts_cutoff": UTC_TYPE,
            "result_utc": UTC_TYPE,
        },
    )


def rows(
    seasons: list[int], games: int = 600, experiments: tuple[str, ...] = ("E1",)
) -> pl.DataFrame:
    return pl.concat(
        [season_rows(s, games, s + k, e) for k, e in enumerate(experiments) for s in seasons]
    )


def starts(frame: pl.DataFrame) -> dict[tuple[str, int], datetime]:
    """Each experiment's fold start per season: its first prediction."""
    firsts = frame.group_by("experiment", "season").agg(pl.col("prediction_utc").min())
    return {(e, s): t for e, s, t in firsts.iter_rows()}
