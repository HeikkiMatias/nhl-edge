"""Point-in-time rules of the closing proxy (#21, ADR 0033): it is read from snapshots taken
before the game's start and after the day's decision snapshot only, and a later snapshot can
replace it only until the start."""

from datetime import timedelta

import polars as pl
from closing_fixtures import MIDDAY, MINUTE, NOW, PRE7, START, pair, quotes

from nhl_edge.market import closing


def test_the_proxy_precedes_the_start_and_follows_the_decision_snapshot() -> None:
    frame = quotes(pair(MIDDAY), pair(PRE7), pair(START - 3 * MINUTE))
    found = closing.proxies(frame, NOW)
    decision = closing.decision_snapshots(frame)["decision_utc"].item()
    assert (found["snapshot_utc"] < found["start_utc"]).all()
    assert (found["snapshot_utc"] > decision).all()


def test_a_snapshot_at_or_after_the_start_never_moves_the_close() -> None:
    # Live prices (the replay drops them, but a quote at the start itself is no close either).
    before = closing.proxies(quotes(pair(MIDDAY), pair(PRE7)), NOW)
    at_start = quotes(pair(MIDDAY), pair(PRE7), pair(START), pair(START + 20 * MINUTE))
    assert closing.proxies(at_start, NOW).equals(before)


def test_a_game_is_marked_only_once_it_has_started() -> None:
    frame = quotes(pair(MIDDAY), pair(PRE7))
    assert closing.proxies(frame, START - timedelta(seconds=1)).is_empty()
    assert closing.flag(frame, START - timedelta(seconds=1))["is_closing_proxy"].sum() == 0
    assert closing.pinnacle_closes(
        frame.with_columns(game_id=pl.lit(1, pl.Int64)), START - timedelta(seconds=1)
    ).is_empty()
