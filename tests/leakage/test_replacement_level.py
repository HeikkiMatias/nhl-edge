"""Point-in-time rules for scripts/replacement_level.py (#134, ADR 0051, hard rule 1): a season's
replacement levels read only earlier seasons, a newcomer's known rating only ratings public before
his game, and a season's predictions never its own results."""

from datetime import timedelta

import numpy as np
import polars as pl
from replacement_fixtures import (
    CANDIDATES,
    DRESSED,
    G2,
    GAMES,
    RATED,
    SEASON,
    START2,
    game_rows,
    rated,
    rl,
)
from replacement_fixtures import levels as constant

EARLIER, LATER = SEASON - 10_001, SEASON + 10_001
SEASONS = (EARLIER, SEASON, LATER)


def _season_rows() -> tuple[pl.DataFrame, pl.DataFrame]:
    seasons = pl.DataFrame(
        {
            "season": list(SEASONS) * 2,
            "player_id": [1, 2, 3, 4, 5, 6],
            "games": [10, 10, 10, 5, 5, 5],
            "role": ["F", "F", "F", "D", "D", "D"],
            "end_level": [-0.1, -0.2, -0.3, -0.1, -0.2, -0.3],
        }
    ).with_columns(pl.col("season").cast(pl.Int32))
    new = pl.DataFrame(
        {
            "season": list(SEASONS) * 2,
            "role": ["F"] * 3 + ["D"] * 3,
            "opener": [False] * 6,
            "prior": [-0.01, -0.02, -0.03] * 2,
            "known": [-0.04, -0.05, -0.06] * 2,
        }
    ).with_columns(pl.col("season").cast(pl.Int32))
    return seasons, new


def _shift(frame: pl.DataFrame, when: pl.Expr, columns: tuple[str, ...]) -> pl.DataFrame:
    return frame.with_columns(
        pl.when(when).then(pl.col(c) + 1.0).otherwise(pl.col(c)).alias(c) for c in columns
    )


def test_a_season_s_levels_never_read_that_season_or_later() -> None:
    seasons, new = _season_rows()
    base = rl.season_levels(SEASON, seasons, new)
    now = pl.col("season") >= SEASON
    moved = rl.season_levels(
        SEASON,
        _shift(seasons, now, ("end_level",)),
        _shift(new, now, ("prior", "known")),
    )
    assert moved == base
    before = pl.col("season") < SEASON
    shifted = rl.season_levels(SEASON, _shift(seasons, before, ("end_level",)), new)
    assert shifted[rl.FRINGE] != base[rl.FRINGE]


def test_a_newcomer_s_rating_counts_only_from_before_his_game() -> None:
    at_start = rl.newcomers(DRESSED, CANDIDATES, RATED, GAMES)
    two = pl.col("player_id") == 2
    # His only earlier rating is as of the game's start: his season prior stands in.
    assert at_start.filter(two, pl.col("game_id") == G2)["known"].item() == -0.05
    earlier = RATED.with_columns(
        as_of_utc=pl.when(two & (pl.col("game_id") == G2))
        .then(pl.lit(START2 - timedelta(minutes=1)))
        .otherwise(pl.col("as_of_utc"))
    )
    before = rl.newcomers(DRESSED, CANDIDATES, earlier, GAMES)
    assert before.filter(two, pl.col("game_id") == G2)["known"].item() == 0.30


def test_a_later_rating_never_reaches_an_earlier_game() -> None:
    later = pl.concat(
        [RATED, rated([(2012020099, SEASON, 3, START2 + timedelta(days=30), 0.9, 0.0)])]
    )
    new = rl.newcomers(DRESSED, CANDIDATES, later, GAMES)
    # 3's season prior from that later row stands in, never its rating.
    assert new.filter(pl.col("player_id") == 3)["known"].item() == 0.0


def test_a_season_s_predictions_never_read_its_own_results() -> None:
    rows = game_rows(SEASONS)
    calendar = rows.select("season", "start_utc")
    by_season = {s: constant(-0.02) for s in SEASONS}

    def predictions(frame: pl.DataFrame) -> pl.DataFrame:
        scored = rl.score(frame, calendar, by_season, (SEASON, LATER))
        return scored.filter(pl.col("season") == LATER).select(
            "game_id", *(f"{k}_{o}" for k in ("p", "rhat") for o in (rl.ZERO, *rl.LEVELS))
        )

    base = predictions(rows)
    own = pl.col("season") == LATER
    flipped = rows.with_columns(
        home_win=pl.when(own).then(1 - pl.col("home_win")).otherwise(pl.col("home_win")),
        r=pl.when(own).then(-pl.col("r")).otherwise(pl.col("r")),
    )
    assert predictions(flipped).equals(base)
    earlier = rows.with_columns(
        r=pl.when(pl.col("season") == SEASON).then(-pl.col("r")).otherwise(pl.col("r"))
    )
    moved = predictions(earlier)
    assert not np.allclose(moved["rhat_zero"].to_numpy(), base["rhat_zero"].to_numpy())
