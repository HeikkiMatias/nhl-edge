"""Point in time for scripts/officials_timing.py (#423, hard rule 1): a right-rail copy fetched at
or after a game's scheduled start never counts, so officials named only once the game is under way
are never taken for known before it, and a copy at 13:30 ET is not before the decision."""

from test_officials_timing import EVENING, A, copied, et, games, ot


def test_a_copy_at_or_after_the_start_never_counts() -> None:
    per_game = ot.measure(
        copied((A, et(12, 46), 0), (A, EVENING, 2), (A, et(22), 2)),
        games((A, EVENING)),
    )
    row = per_game.row(0, named=True)
    assert row["copies"] == 1 and row["first_seen_min"] is None
    assert not row["known_by_decision"]


def test_a_copy_at_the_decision_bound_is_not_before_it() -> None:
    per_game = ot.measure(copied((A, et(13, 30), 2)), games((A, EVENING)))
    row = per_game.row(0, named=True)
    assert not row["known_by_decision"] and not row["covered"]
