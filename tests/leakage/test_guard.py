"""The market move guard (#142, ADR 0029) reads only quotes taken before the decision, for games
not yet started, and its threshold reads only the 2011-12 to 2017-18 prices."""

from datetime import UTC, datetime, timedelta

import market_history
import polars as pl
from guard_quotes import DECISION, START, quotes, snapshots

from nhl_edge.audit.sbr import moneylines, moves
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
    odds, _ = market_history.seasons(list(guard.THRESHOLD_SEASONS), games=60)
    later, later_games = market_history.seasons([20212022, 20222023], games=200)
    # Plant openers far from their close in the later seasons, so a threshold that read them
    # would move.
    for game_id in later_games["game_id"].to_list()[::4]:
        later = market_history.implausible_opener(later, game_id)
    every = pl.concat([odds, later])
    moved = moves(moneylines(every))["move"].quantile(guard.QUANTILE, "linear")
    assert isinstance(moved, float)
    assert round(moved, 4) != guard.threshold(odds)
    assert guard.threshold(every) == guard.threshold(odds)


def test_a_slot_of_an_earlier_day_is_never_paired_with_the_decision_days() -> None:
    # The event was listed the day before: yesterday's morning quote must not stand in for a
    # missing game-day morning quote.
    yesterday = datetime(2026, 9, 30, 11, 5, tzinfo=UTC)
    midday = datetime(2026, 10, 1, 16, 45, tzinfo=UTC)
    frame = snapshots(
        quotes("a", "morning", yesterday, 1.60, 2.40), quotes("a", "midday", midday, 2.00, 1.90)
    )
    assert guard.live_moves(frame, DECISION).is_empty()
