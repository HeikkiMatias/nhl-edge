"""Helpers for scripts/goals_attempts.py's tests (#420): the script as a module, and team strength's
SYNTHETIC league (team_fixtures) with each shot's event type and flags, and made-up blocked
attempts, as the lake's shots and blocked_shots tables hold them."""

import importlib.util
from datetime import timedelta
from pathlib import Path
from types import ModuleType

import numpy as np
import polars as pl
from team_fixtures import league as team_league

UTC_TYPE = pl.Datetime("us", "UTC")


def load() -> ModuleType:
    """scripts/goals_attempts.py as a module."""
    path = Path(__file__).parents[2] / "scripts" / "goals_attempts.py"
    spec = importlib.util.spec_from_file_location("goals_attempts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def league(seed: int = 5) -> dict[str, pl.DataFrame]:
    """SYNTHETIC: team strength's league (two seasons, six teams) with each shot a goal at its xG,
    the flags the lake's shots carry, and per team-game two 5v5 blocked attempts with the
    shooting team's goalie in, one 5v4 and one teammate's block, which only the 5v5 ones count."""
    found = team_league()
    rng = np.random.default_rng(seed)
    games = found["games"]
    public = games.select("game_id", "season", "observed_utc")
    xg = found["shot_xg"].select("game_id", "event_id", "xg")
    shots = (
        found["shots"]
        .join(xg, on=["game_id", "event_id"])
        .join(public, on="game_id")
        .with_columns(
            event_type=pl.when(pl.Series(rng.random(found["shots"].height)) < pl.col("xg"))
            .then(pl.lit("goal"))
            .otherwise(pl.lit("shot-on-goal")),
            is_empty_net=pl.lit(False),
            is_penalty_shot=pl.lit(False),
        )
        .drop("xg")
    )
    rows = []
    for game in games.iter_rows(named=True):
        for team, is_home in ((game["home"], True), (game["away"], False)):
            for event, (for_, against, reason) in enumerate(
                ((5, 5, None), (5, 5, None), (5, 4, None), (5, 5, "teammate-blocked")),
                start=1000 if is_home else 2000,
            ):
                side = (for_, against) if is_home else (against, for_)
                rows.append(
                    {
                        "game_id": game["game_id"],
                        "season": game["season"],
                        "event_id": event,
                        "team": team,
                        "is_home": is_home,
                        "skaters_for": for_,
                        "skaters_against": against,
                        "situation_code": f"1{side[1]}{side[0]}1",
                        "is_empty_net": False,
                        "reason": reason,
                        "observed_utc": game["observed_utc"],
                    }
                )
    blocked = pl.DataFrame(rows, schema_overrides={"reason": pl.String}).with_columns(
        pl.col("game_id").cast(pl.Int64), pl.col("season").cast(pl.Int32)
    )
    return found | {"shots": shots, "blocked_shots": blocked}


def history(found: dict[str, pl.DataFrame] | None = None) -> pl.DataFrame:
    """The league's team-games as the script builds them."""
    found = league() if found is None else found
    return load().team_games(
        found["shots"], found["blocked_shots"], found["shot_xg"], found["strength_time"]
    )


def later(frame: pl.DataFrame) -> pl.DataFrame:
    """A frame's rows two seasons on, after the league's last: the same games, renumbered, and
    dated, started and public two years later."""
    shift = timedelta(days=730)
    moved = [c for c in ("game_date", "start_utc", "observed_utc") if c in frame.columns]
    return frame.with_columns(
        game_id=pl.col("game_id") + 1_000_000_000,
        season=pl.col("season") + 20002,
        **{c: pl.col(c) + shift for c in moved},
    )
