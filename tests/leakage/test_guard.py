"""The market move guard (#142, ADR 0029) reads only quotes taken before the decision, for games
not yet started, and its threshold reads only the 2011-12 to 2017-18 prices."""

from datetime import UTC, datetime, timedelta

import market_history
import polars as pl
from guard_quotes import DECISION, START, quotes, snapshots

from nhl_edge.betting import guard


def test_a_snapshot_taken_at_or_after_the_decision_is_not_read() -> None:
    morning = datetime(2026, 10, 1, 11, 5, tzinfo=UTC)
    midday = datetime(2026, 10, 1, 16, 45, tzinfo=UTC)
    base = snapshots(
        quotes("a", "morning", morning, 1.80, 2.10), quotes("a", "midday", midday, 2.00, 1.90)
    )
    later = snapshots(quotes("a", "midday", DECISION, 3.00, 1.40))
    assert guard.live_moves(pl.concat([base, later]), DECISION).equals(
        guard.live_moves(base, DECISION)
    )


def test_a_game_already_started_has_no_move() -> None:
    morning = datetime(2026, 10, 1, 11, 5, tzinfo=UTC)
    started = snapshots(
        quotes("a", "morning", morning, 1.80, 2.10), quotes("a", "midday", morning, 2.00, 1.90)
    )
    assert guard.live_moves(started, START + timedelta(minutes=1)).is_empty()


def test_the_threshold_never_reads_a_development_or_held_out_season() -> None:
    odds, _ = market_history.seasons([20132014, 20142015], games=200)
    later, _ = market_history.seasons([20212022, 20222023], games=200, intercept=2.0)
    assert guard.threshold(pl.concat([odds, later])) == guard.threshold(odds)
