"""Helpers for scripts/totals_history.py's tests (#359): the script as a module, and SYNTHETIC
tables: SBR totals quotes, B3's split inputs, goalie candidates and gammas, and a scored frame."""

import importlib.util
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import ModuleType

import numpy as np
import polars as pl

UTC_TYPE = pl.Datetime("us", "UTC")
D = date(2013, 11, 1)
PREDICTION = datetime.combine(D, time(14, 0, 1), UTC)
OPENED = datetime.combine(D, time(14), UTC)
START = datetime.combine(D, time(23), UTC)
SEASONS = (20122013, 20132014, 20142015, 20152016)


def load() -> ModuleType:
    """scripts/totals_history.py as a module."""
    path = Path(__file__).parents[2] / "scripts" / "totals_history.py"
    spec = importlib.util.spec_from_file_location("totals_history", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sbr(rows: list[tuple[int, str, str, float, float, datetime]]) -> pl.DataFrame:
    """SBR totals prices: (game_id, quote, side, line, price_decimal, assumed_available_utc)."""
    return pl.DataFrame(
        {
            "game_id": [r[0] for r in rows],
            "market": ["totals"] * len(rows),
            "quote": [r[1] for r in rows],
            "side": [r[2] for r in rows],
            "line": [r[3] for r in rows],
            "price_decimal": [r[4] for r in rows],
            "assumed_available_utc": [r[5] for r in rows],
        }
    ).with_columns(pl.col("assumed_available_utc").cast(UTC_TYPE))


def game_quotes(
    game_id: int,
    open_line: float = 5.5,
    open_prices: tuple[float, float] = (1.95, 1.87),
    close_line: float = 5.5,
    close_prices: tuple[float, float] = (2.0, 1.83),
    opened: datetime = OPENED,
) -> list[tuple[int, str, str, float, float, datetime]]:
    """A game's opener and close, the over's price first."""
    return [
        (game_id, "open", "over", open_line, open_prices[0], opened),
        (game_id, "open", "under", open_line, open_prices[1], opened),
        (game_id, "close", "over", close_line, close_prices[0], START),
        (game_id, "close", "under", close_line, close_prices[1], START),
    ]


def moments(game_ids: list[int], at: datetime = PREDICTION) -> pl.DataFrame:
    return pl.DataFrame({"game_id": game_ids, "prediction_utc": [at] * len(game_ids)}).with_columns(
        pl.col("prediction_utc").cast(UTC_TYPE)
    )


def split(
    rows: list[tuple[int, str, str, float, float, float, float]] | None = None,
    observed: datetime = PREDICTION - timedelta(hours=1),
) -> pl.DataFrame:
    """B3's split inputs: (game_id, home, away, home_five, home_special, away_five,
    away_special), raw the sum of five and special, bases 1.1 at home and 0.9 away."""
    rows = rows or [(1, "BOS", "NYR", 2.0, 0.6, 1.8, 0.5)]
    frame = pl.DataFrame(
        {
            "game_id": [r[0] for r in rows],
            "home": [r[1] for r in rows],
            "away": [r[2] for r in rows],
            "home_five": [r[3] for r in rows],
            "home_special": [r[4] for r in rows],
            "away_five": [r[5] for r in rows],
            "away_special": [r[6] for r in rows],
        }
    )
    return frame.with_columns(
        home_raw=pl.col("home_five") + pl.col("home_special"),
        away_raw=pl.col("away_five") + pl.col("away_special"),
        home_base=pl.lit(1.1),
        away_base=pl.lit(0.9),
        observed_utc=pl.lit(observed, dtype=UTC_TYPE),
    )


def pool(
    rows: list[tuple[int, str, int, float]] | None = None,
    observed: datetime = PREDICTION - timedelta(hours=1),
) -> pl.DataFrame:
    """Goalie candidates: (game_id, team, goalie_id, p_start)."""
    rows = rows or [(1, "BOS", 30, 0.8), (1, "BOS", 31, 0.2), (1, "NYR", 40, 1.0)]
    return pl.DataFrame(
        {
            "game_id": [r[0] for r in rows],
            "team": [r[1] for r in rows],
            "goalie_id": [r[2] for r in rows],
            "p_start": [r[3] for r in rows],
            "observed_utc": [observed] * len(rows),
        }
    ).with_columns(pl.col("observed_utc").cast(UTC_TYPE))


def gammas(rows: list[tuple[int, str, int, float]] | None = None) -> pl.DataFrame:
    """Each attacking team's gamma against an opposing candidate: (game_id, team, goalie_id,
    gamma). BOS shoots on NYR's 40, NYR on BOS's 30 and 31."""
    rows = rows or [(1, "BOS", 40, 0.95), (1, "NYR", 30, 0.9), (1, "NYR", 31, 1.2)]
    return pl.DataFrame(
        {
            "game_id": [r[0] for r in rows],
            "team": [r[1] for r in rows],
            "goalie_id": [r[2] for r in rows],
            "gamma": [r[3] for r in rows],
        }
    )


def scored_frame(seed: int = 7, per_season: int = 400, signal: float = 0.3) -> pl.DataFrame:
    """A synthetic scored frame over SEASONS: the move y depends on the projection's distance
    from its mean by signal, and the over's logit beyond the close's by ten times signal."""
    rng = np.random.default_rng(seed)
    parts = []
    for index, season in enumerate(SEASONS):
        first = date(2012 + index, 10, 10)
        days = [first + timedelta(days=int(d)) for d in rng.integers(0, 170, per_season)]
        total = rng.normal(5.3, 0.3, per_season)
        mu_o = rng.normal(5.5, 0.2, per_season)
        y = signal * (total - 5.3) + rng.normal(0, 0.1, per_season)
        mu_c = mu_o + y
        q_c = 0.8 * (mu_c - 5.5)
        p = 1 / (1 + np.exp(-(q_c + 10 * signal * (total - 5.3))))
        over = (rng.random(per_season) < p).astype(np.int8)
        pushed = rng.random(per_season) < 0.05
        parts.append(
            pl.DataFrame(
                {
                    "game_id": [season * 10_000 + i for i in range(per_season)],
                    "season": [season] * per_season,
                    "game_date": days,
                    "rested": rng.random(per_season) < 0.7,
                    "total": total,
                    "total_five": total * 0.6,
                    "total_special": total * 0.25,
                    "total_finishing": total * 0.1,
                    "total_goalie": total * 0.05 + rng.normal(0, 0.05, per_season),
                    "mu_o": mu_o,
                    "mu_c": mu_c,
                    "q_c": q_c,
                    "y": y,
                    "line_o": [5.5] * per_season,
                    "line_c": [5.5 if i % 9 else 5.0 for i in range(per_season)],
                    "goals": rng.poisson(5.5, per_season).astype(np.int32),
                    "over": [None if pushed[i] else int(over[i]) for i in range(per_season)],
                }
            )
        )
    frame = pl.concat(parts).with_columns(
        pl.col("season").cast(pl.Int32), pl.col("over").cast(pl.Int8)
    )
    return frame.with_columns(
        start_utc=pl.col("game_date").dt.combine(time(23)).dt.replace_time_zone("UTC"),
        prediction_utc=pl.col("game_date").dt.combine(time(14, 0, 1)).dt.replace_time_zone("UTC"),
        result_utc=(pl.col("game_date") + timedelta(days=1))
        .dt.combine(time(10))
        .dt.replace_time_zone("UTC"),
    ).sort("game_id")
