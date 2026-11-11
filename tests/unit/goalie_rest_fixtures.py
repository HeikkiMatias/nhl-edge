"""Helpers for scripts/goalie_rest.py's tests (#418): the script as a module, made-up boxscore
starts, the synthetic league as the yardstick reads it, and made-up shots."""

import importlib.util
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import numpy as np
import polars as pl
import yardstick_fixtures as yf

from nhl_edge.backtest import yardstick as ys

UTC_TYPE = pl.Datetime("us", "UTC")


def load() -> ModuleType:
    """scripts/goalie_rest.py as a module."""
    path = Path(__file__).parents[2] / "scripts" / "goalie_rest.py"
    spec = importlib.util.spec_from_file_location("goalie_rest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def morning_after(day: date) -> datetime:
    """When a game's boxscore counts as public: 10:00 UTC the next morning (ADR 0003)."""
    return datetime.combine(day + timedelta(days=1), time(10), UTC)


def started(rows: Sequence[tuple[int, date, int]]) -> pl.DataFrame:
    """SYNTHETIC starts as starts() gives them: (goalie_id, date, game_id unused)."""
    return pl.DataFrame(
        {
            "goalie_id": [g for g, _, _ in rows],
            "start_date": [d for _, d, _ in rows],
            "start_utc": [morning_after(d) for _, d, _ in rows],
        },
        schema={"goalie_id": pl.Int64, "start_date": pl.Date, "start_utc": UTC_TYPE},
    )


def asks(rows: Sequence[tuple[int, date, datetime]]) -> pl.DataFrame:
    """SYNTHETIC asks: (goalie_id, tonight's date, as-of time)."""
    return pl.DataFrame(
        {
            "goalie_id": [g for g, _, _ in rows],
            "game_date": [d for _, d, _ in rows],
            "as_of_utc": [t for _, _, t in rows],
        },
        schema={"goalie_id": pl.Int64, "game_date": pl.Date, "as_of_utc": UTC_TYPE},
    )


def league_read() -> ys.Read:
    """The synthetic league to 2014-15 as ys.read gives it (yardstick_fixtures.league)."""
    teams, players, sbr = yf.league()
    teams, players = ys.through(teams, 20142015), ys.through(players, 20142015)
    market = ys.market(sbr, players.games)
    found = ys.features(teams, players, market.select("game_id", "prediction_utc"))
    return ys.Read(players.games, teams, players, ys.rows(market, found, players.games))


def shots(
    seasons: Sequence[int] = (20122013, 20132014, 20142015),
    games: int = 120,
    tired_effect: float = 1.0,
    seed: int = 3,
) -> pl.DataFrame:
    """SYNTHETIC step 1 shots: per season, games on consecutive dates, 30 shots each. The starter
    started yesterday in every third game (band 1, starts7 2), else he rested 2 days (band 2,
    starts7 1). A goal's log-odds is logit(xG) plus tired_effect on band 1."""
    rng = np.random.default_rng(seed)
    rows = []
    for season in seasons:
        year = season // 10_000
        for g in range(games):
            day = date(year, 10, 10) + timedelta(days=g)
            tired = g % 3 == 0
            for s in range(30):
                xg = float(rng.uniform(0.02, 0.3))
                logit = np.log(xg / (1 - xg)) + (tired_effect if tired else 0.0)
                rows.append(
                    {
                        "game_id": year * 1_000_000 + 20_000 + g + 1,
                        "season": season,
                        "game_date": day,
                        "goalie_id": 1 + g % 2,
                        "observed_utc": morning_after(day),
                        "goal": float(rng.random() < 1 / (1 + np.exp(-logit))),
                        "xg": xg,
                        "band": "1" if tired else "2",
                        "logit_xg": float(np.log(xg / (1 - xg))),
                        "log_gamma": 0.0,
                        "defending_home": float(s % 2),
                        "team_b2b": 1.0 if tired else float(g % 5 == 1),
                        "band_1": 1.0 if tired else 0.0,
                        "band_3": 0.0,
                        "band_4_6": 0.0,
                        "band_7_plus": 0.0,
                        "starts7": 2.0 if tired else 1.0,
                    }
                )
    return pl.DataFrame(rows).with_columns(
        pl.col("season").cast(pl.Int32), pl.col("observed_utc").cast(UTC_TYPE)
    )


def calendar(frame: pl.DataFrame) -> pl.DataFrame:
    """The yardstick rows' calendar that ys.fold_start reads: each season's first start."""
    return (
        frame.group_by("season")
        .agg(pl.col("game_date").min())
        .select(
            "season",
            start_utc=pl.col("game_date").cast(UTC_TYPE) + timedelta(hours=23),
        )
    )


D = date(2013, 11, 1)
LATE = datetime.combine(D, time(23), UTC)


def step1_read() -> Any:
    """Game 1 yesterday (NYR at home to PHI, G1 starts), game 2 tonight (NYR at home to BOS: G1
    starts for NYR, G2 for BOS, G3 relieves for BOS)."""
    season = pl.lit(20132014, dtype=pl.Int32)
    games = pl.DataFrame(
        {
            "game_id": [1, 2],
            "game_date": [D - timedelta(days=1), D],
            "start_utc": [LATE - timedelta(days=1), LATE],
            "home": ["NYR", "NYR"],
            "away": ["PHI", "BOS"],
        },
        schema_overrides={"start_utc": UTC_TYPE},
    ).with_columns(season=season)
    lineups = pl.DataFrame(
        {
            "game_id": [1, 1, 2, 2, 2],
            "game_date": [D - timedelta(days=1), D - timedelta(days=1), D, D, D],
            "team": ["NYR", "PHI", "NYR", "BOS", "BOS"],
            "player_id": [1, 9, 1, 2, 3],
            "role": ["G"] * 5,
            "starting_goalie": [True, True, True, True, False],
            "observed_utc": [morning_after(D - timedelta(days=1))] * 2 + [morning_after(D)] * 3,
        },
        schema_overrides={"observed_utc": UTC_TYPE},
    ).with_columns(season=season)
    gammas = pl.DataFrame(
        {
            "game_id": [2, 2],
            "team": ["BOS", "NYR"],
            "goalie_id": [1, 2],
            "gamma": [0.9, 1.2],
            # BOS's gamma against G1 is known before the start; NYR's against G2 only after it.
            "observed_utc": [LATE - timedelta(hours=9), LATE + timedelta(hours=1)],
        },
        schema_overrides={"observed_utc": UTC_TYPE},
    )
    return SimpleNamespace(
        games=games,
        # The calendar ys.fold_start reads: each season's first start.
        frame=games,
        b2_tables=SimpleNamespace(actual_lineups=lineups),
        b3_tables=SimpleNamespace(goal_multipliers=gammas),
    )


def step1_shots() -> tuple[pl.DataFrame, pl.DataFrame]:
    """Game 2's shots: BOS at G1 (kept), NYR at G2 (kept), NYR at the relief G3 (left out), and
    BOS at G1 without an xG (left out)."""
    shots = pl.DataFrame(
        {
            "game_id": [2, 2, 2, 2],
            "season": [20132014] * 4,
            "game_date": [D] * 4,
            "event_id": [1, 2, 3, 4],
            "team": ["BOS", "NYR", "NYR", "BOS"],
            "is_home": [False, True, True, False],
            "goalie_id": [1, 2, 3, 1],
            "is_goal": [True, False, False, False],
            "observed_utc": [morning_after(D)] * 4,
        },
        schema_overrides={"season": pl.Int32, "observed_utc": UTC_TYPE},
    )
    xg = pl.DataFrame(
        {
            "game_id": [2, 2, 2],
            "season": [20132014] * 3,
            "event_id": [1, 2, 3],
            "xg": [0.1, 0.2, 0.3],
            "train_cutoff": [LATE - timedelta(days=200)] * 3,
        },
        schema_overrides={"season": pl.Int32, "train_cutoff": UTC_TYPE},
    )
    return shots, xg
