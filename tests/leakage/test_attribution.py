"""Point-in-time rules for the live bets' usual levels (#193). Each training game's B3 parts come
from its own fold's fit at its prediction time: they never read the game's own result, and the
levels are refused if any row or fit behind them was known after the live fold starts."""

from datetime import UTC, datetime

import polars as pl
import pytest
from b3_fixtures import league
from polars.testing import assert_frame_equal

from nhl_edge.backtest.walk_forward import fold_start
from nhl_edge.game import b3
from nhl_edge.live import attribution as la

LEAGUE = league()
TEST = 20182019
START = fold_start(LEAGUE.games.select("season", "start_utc"), TEST)
# The test season's games as training games, with a market price each.
HISTORY = (
    LEAGUE.games.filter(pl.col("season") == TEST)
    .select("season", "game_id", prediction_utc="start_utc")
    .with_columns(logit_mkt=pl.lit(0.1))
)


def parts(tables: b3.Tables = LEAGUE) -> pl.DataFrame:
    found, _ = la.season_parts(tables, HISTORY, TEST, START)
    return found.sort("game_id")


def test_a_training_games_own_result_never_moves_its_parts() -> None:
    tested = pl.col("season") == TEST
    games = LEAGUE.games.with_columns(
        home_score=pl.when(tested).then(pl.col("away_score")).otherwise(pl.col("home_score")),
        away_score=pl.when(tested).then(pl.col("home_score")).otherwise(pl.col("away_score")),
    )
    flipped = b3.Tables(**{**LEAGUE.__dict__, "games": games})
    assert_frame_equal(parts(flipped), parts(), rel_tol=1e-12, abs_tol=1e-12)


def test_the_levels_refuse_a_row_known_once_the_live_fold_starts() -> None:
    found = parts()
    now = datetime(2026, 10, 8, tzinfo=UTC)
    live_start = datetime(2026, 9, 29, 21, tzinfo=UTC)
    record = la.artifact("attribution-levels-x", "blend-live-x", live_start, found, [], now)
    assert datetime.fromisoformat(record["train_cutoff"]) < live_start
    with pytest.raises(ValueError, match="after"):
        la.artifact("attribution-levels-x", "blend-live-x", START, found, [START], now)
