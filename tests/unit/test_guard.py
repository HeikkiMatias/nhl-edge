from datetime import UTC, datetime

import market_history
import polars as pl
import pytest
from guard_quotes import DECISION, quotes, snapshots

from nhl_edge.betting import guard


def test_the_move_against_a_bet_is_the_fall_of_its_sides_probability() -> None:
    bets = pl.DataFrame(
        {
            "side": ["home", "away", "home", "away"],
            "p_morning": [0.60, 0.60, 0.50, 0.40],
            "p_decision": [0.52, 0.52, 0.53, 0.47],
        }
    )
    checked = guard.guard(bets)
    assert checked["moved_against"].to_list() == pytest.approx([0.08, -0.08, -0.03, 0.07])
    # A home bet whose price lengthened 8 points, and an away bet whose side fell 7: both skipped.
    assert checked["guarded"].to_list() == [True, False, False, True]


def test_the_threshold_is_the_95th_percentile_of_moves_in_its_seasons_only() -> None:
    odds, _ = market_history.seasons(list(guard.THRESHOLD_SEASONS), games=60)
    found = guard.threshold(odds)
    assert 0 < found < 0.2
    later, _ = market_history.seasons([20182019], games=200, intercept=1.0)
    # A development season's moves never move it.
    assert guard.threshold(pl.concat([odds, later])) == found
    with pytest.raises(ValueError, match="no SBR openers and closes"):
        guard.threshold(later)


def test_the_threshold_refuses_prices_lacking_one_of_its_seasons() -> None:
    odds, _ = market_history.seasons([20112012, 20122013], games=50)
    with pytest.raises(ValueError, match="20132014"):
        guard.threshold(odds)


def test_live_moves_read_the_books_morning_and_midday_snapshots() -> None:
    morning = datetime(2026, 10, 1, 11, 5, tzinfo=UTC)
    midday = datetime(2026, 10, 1, 16, 45, tzinfo=UTC)
    frame = snapshots(
        quotes("a", "morning", morning, 1.80, 2.10),
        quotes("a", "midday", midday, 2.00, 1.90),
        quotes("a", "midday", midday, 1.70, 2.30, book="betfair"),
        quotes("b", "morning", morning, 1.90, 2.00),
    )
    moved = guard.live_moves(frame, DECISION)
    # Event b has no midday price, so the guard has nothing to compare.
    assert moved["event_id"].to_list() == ["a"]
    row = moved.row(0, named=True)
    assert row["p_morning"] == pytest.approx((1 / 1.80) / (1 / 1.80 + 1 / 2.10))
    assert row["p_decision"] == pytest.approx((1 / 2.00) / (1 / 2.00 + 1 / 1.90))
    assert (row["morning_utc"], row["decision_utc"]) == (morning, midday)
