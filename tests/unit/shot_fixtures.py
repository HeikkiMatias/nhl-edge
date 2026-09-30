"""Synthetic shots for the xG tests (#73): valid Shots rows whose goals follow known effects, so a
fit can be checked against them. Each season's shots are spread over October to March, each game
date public at 10:00 UTC the next morning, and each season's first game starts at 23:00 UTC on
October 5."""

from datetime import UTC, date, datetime, time, timedelta

import numpy as np
import polars as pl

from nhl_edge.lake.schemas import Shots, dtypes

SHOT_TYPES = ("wrist", "snap", "slap", "backhand", "tip-in")
# Log-odds of a goal: a base, per foot of distance, for a rebound and for a rush, and per season
# after the first (scoring drifts up).
BASE, PER_FOOT, REBOUND, RUSH, PER_SEASON = -0.2, -0.06, 1.0, 0.5, 0.1


def first_day(season: int) -> date:
    return date(season // 10000, 10, 5)


def games_of(seasons: list[int]) -> pl.DataFrame:
    """Each season's first start, as the games table gives it to fold_start."""
    return pl.DataFrame(
        {
            "season": seasons,
            "start_utc": [datetime.combine(first_day(s), time(23), UTC) for s in seasons],
        },
        schema={"season": pl.Int32, "start_utc": pl.Datetime("us", "UTC")},
    )


def synthetic_shots(
    seasons: list[int],
    per_season: int = 4000,
    seed: int = 7,
    rebound_by_season: list[float] | None = None,
) -> pl.DataFrame:
    """Shots of each season. rebound_by_season gives each season's rebound log-odds in place of
    REBOUND, so rebounds can convert less every season."""
    rng = np.random.default_rng(seed)
    frames = []
    for index, season in enumerate(seasons):
        n = per_season
        days = rng.integers(0, 180, n)
        game_dates = [first_day(season) + timedelta(days=int(d)) for d in days]
        x = rng.integers(-20, 89, n)
        y = rng.integers(-40, 41, n)
        distance = np.hypot(89 - x, y)
        rebound = rng.random(n) < 0.08
        rush = rng.random(n) < 0.1
        rebound_effect = rebound_by_season[index] if rebound_by_season else REBOUND
        logit = (
            BASE + PER_FOOT * distance + rebound_effect * rebound + RUSH * rush + PER_SEASON * index
        )
        is_goal = rng.random(n) < 1 / (1 + np.exp(-logit))
        penalty = rng.random(n) < 0.01
        empty = ~penalty & (rng.random(n) < 0.02)
        period = rng.integers(1, 4, n)
        frames.append(
            pl.DataFrame(
                {
                    "game_id": [season // 10000 * 1_000_000 + 20_000 + int(d) for d in days],
                    "season": season,
                    "game_date": game_dates,
                    "event_id": np.arange(n) + 1,
                    "sort_order": np.arange(n) + 1,
                    "period": period,
                    "seconds": (period - 1) * 1200 + rng.integers(0, 1200, n),
                    "team": "MTL",
                    "is_home": True,
                    "event_type": np.where(is_goal, "goal", "shot-on-goal"),
                    "is_goal": is_goal,
                    "shooter_id": 8_470_000,
                    "goalie_id": pl.Series(
                        [None if e else 8_471_000 for e in empty], dtype=pl.Int64
                    ),
                    "shot_type": rng.choice(SHOT_TYPES, n),
                    "zone": "O",
                    "x": x,
                    "y": y,
                    "skaters_for": np.where(penalty, 1, 5),
                    "skaters_against": np.where(penalty, 0, 5),
                    "strength": np.where(penalty, "1v0", "5v5"),
                    "is_empty_net": empty,
                    "is_penalty_shot": penalty,
                    "situation_code": np.where(penalty, "1010", np.where(empty, "1550", "1551")),
                    "strength_source": "situation_code",
                    "prev_event_type": np.where(rebound, "shot-on-goal", "faceoff"),
                    "prev_seconds": np.where(rebound | rush, 2, 20),
                    "prev_by_shooting_team": True,
                    "prev_zone": np.where(rush, "N", "O"),
                    "observed_utc": [
                        datetime.combine(d + timedelta(days=1), time(10), UTC) for d in game_dates
                    ],
                    "raw_key": "nhl/play-by-play/synthetic",
                },
                strict=False,
            )
        )
    shots = pl.concat(frames).cast(dtypes(Shots))  # type: ignore[arg-type]
    return Shots.validate(shots.unique(["game_id", "event_id"]).sort("game_id", "event_id"))
