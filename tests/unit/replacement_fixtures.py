"""scripts/replacement_level.py (#134, ADR 0051) loaded as a module, and small frames for its
tests: two teams' first games of a season with candidates, newcomers and ratings by hand, and
synthetic game rows with known replacement levels."""

import importlib.util
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import ModuleType

import numpy as np
import polars as pl


def _load() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "replacement_level.py"
    spec = importlib.util.spec_from_file_location("replacement_level", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rl = _load()
SEASON = 20122013
UTC_TYPE = pl.Datetime("us", "UTC")


def at(day: date, hour: int = 23) -> datetime:
    return datetime.combine(day, time(hour), UTC)


# BOS at TOR on two nights: the first is each team's first game of the season.
G1, G2 = 2012020001, 2012020002
START1, START2 = at(date(2012, 10, 10)), at(date(2012, 10, 12))
GAMES = pl.DataFrame(
    {
        "game_id": [G1, G2],
        "season": [SEASON, SEASON],
        "game_date": [date(2012, 10, 10), date(2012, 10, 12)],
        "start_utc": [START1, START2],
        "home": ["TOR", "TOR"],
        "away": ["BOS", "BOS"],
    }
).with_columns(pl.col("season").cast(pl.Int32), pl.col("start_utc").cast(UTC_TYPE))
# TOR's skaters: 1 dresses both nights and is a candidate on the second; 2 (D) and 3 (F) dress
# only on the second, without being candidates.
DRESSED = pl.DataFrame(
    {
        "game_id": [G1, G2, G2, G2],
        "season": [SEASON] * 4,
        "team": ["TOR"] * 4,
        "player_id": [1, 1, 2, 3],
        "role": ["F", "F", "D", "F"],
    }
).with_columns(pl.col("season").cast(pl.Int32))
CANDIDATES = pl.DataFrame({"game_id": [G2], "player_id": [1]})


def rated(rows: list[tuple[int, int, int, datetime, float, float]]) -> pl.DataFrame:
    """five_ratings()-shaped rows: game_id, season, player_id, as_of_utc, level, prior."""
    return pl.DataFrame(
        rows,
        schema={
            "game_id": pl.Int64,
            "season": pl.Int32,
            "player_id": pl.Int64,
            "as_of_utc": UTC_TYPE,
            "level": pl.Float64,
            "prior": pl.Float64,
        },
        orient="row",
    )


# 1 was rated last season (another team's game), 2 is rated as of the second night's start (not
# before it) and again later in the season, 3 never.
RATED = rated(
    [
        (2011021000, 20112012, 1, at(date(2012, 4, 1), 14), 0.10, 0.02),
        (G2, SEASON, 2, START2, 0.30, -0.05),
        (2012020050, SEASON, 2, at(date(2012, 11, 20), 14), 0.40, -0.05),
    ]
)


def game_rows(seasons: tuple[int, ...], games: int = 120, seed: int = 0) -> pl.DataFrame:
    """game_rows()-shaped rows with a result and r, for score(): each season's games two days
    apart from October 10, results public the morning after, with random lineups and
    replacement minutes."""
    rng = np.random.default_rng(seed)
    rows = []
    for season in seasons:
        first = date(season // 10_000, 10, 10)
        for k in range(games):
            day = first + timedelta(days=2 * k)
            h_all, a_all = 240.0, 240.0
            spare = rng.uniform(0, 12, size=4)
            rows.append(
                {
                    "game_id": season // 10_000 * 1_000_000 + 20_000 + k,
                    "season": season,
                    "game_date": day,
                    "start_utc": at(day),
                    "result_utc": at(day + timedelta(days=1), 10),
                    "h_cand_sum": float(rng.normal(0, 3)),
                    "a_cand_sum": float(rng.normal(0, 3)),
                    "h_spare_F": spare[0],
                    "h_spare_D": spare[1],
                    "a_spare_F": spare[2],
                    "a_spare_D": spare[3],
                    "h_all_min": h_all,
                    "a_all_min": a_all,
                    "h_opener": k == 0,
                    "a_opener": k == 0,
                    "opener": k == 0,
                    "spare_share": float(max(spare[:2].sum() / h_all, spare[2:].sum() / a_all)),
                    "home_win": int(rng.integers(0, 2)),
                    "r": float(rng.normal(0, 1)),
                }
            )
    frame = pl.DataFrame(rows).with_columns(
        pl.col("season").cast(pl.Int32),
        pl.col("start_utc", "result_utc").cast(UTC_TYPE),
        pl.col("home_win").cast(pl.Int8),
    )
    hours = (pl.col("h_all_min") + pl.col("a_all_min")) / 2 / rl.ON_ICE / 60
    return frame.with_columns(hours=hours).with_columns(
        x0=pl.col("hours") * (rl.on_ice("h", "cand_sum") - rl.on_ice("a", "cand_sum"))
    )


def levels(value: float) -> dict[str, dict[str, float]]:
    """season_levels()-shaped levels, every option and role at value."""
    keys = (rl.FRINGE, rl.PRIOR, f"{rl.NEWCOMER}_in", f"{rl.NEWCOMER}_open")
    return {k: {r: value for r in rl.ROLES} for k in keys}
