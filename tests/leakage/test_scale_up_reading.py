"""ADR 0047's reading is taken as of its clock (#322): the weekly report's, once Sunday's results
are public. Nothing observed at or after it, neither a fill, a quote nor a stop, changes it, so a
run after the clock reads what the weekly report would have read."""

from datetime import date, timedelta
from pathlib import Path

import fill_fixtures as ff
import polars as pl
import predict_fixtures as pf

from nhl_edge.live import bundle as lb
from nhl_edge.live import fills as lf
from nhl_edge.live import placement as pm
from nhl_edge.live import scale_up as su

TAKER = ff.fill(ff.PLACED, "1.00", "0.5000", "0.0175", True)
MAKER = ff.fill(ff.PROXY - timedelta(hours=2), "2.00", "0.4800", "0.0044", False)


def test_nothing_observed_at_or_after_the_clock_changes_the_reading(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    ff.record(store, [TAKER, MAKER], env=pm.LIVE)
    found = lf.fills(store, pm.LIVE)
    assert su.reading_date(found) == date(2026, 12, 28)
    day = date(2026, 12, 28)
    at = su.clock(day)
    taken = su.take(found, ff.odds(), ff.TEAMS, [], day)
    assert taken[lf.TAKER]["valued"] == 1 and taken[lf.MAKER]["valued"] == 1
    # A fill observed at the clock, of a game after it, isn't read.
    later = found.head(1).with_columns(
        game_id=pl.lit(2026020999, dtype=pl.Int64),
        game_date=pl.lit(day),
        start_utc=pl.lit(at + timedelta(hours=13), dtype=lf.FILL_SCHEMA["start_utc"]),
        fill_utc=pl.lit(at, dtype=lf.FILL_SCHEMA["fill_utc"]),
    )
    # Pinnacle quoting the game again after the clock is no reference either.
    odds = ff.odds(ff.PROXY, at, at + timedelta(days=1))
    # A stop alerted on the reading day or after it isn't one before the reading.
    stops = [day, day + timedelta(days=3)]
    again = su.take(pl.concat([found, later]), odds, ff.TEAMS, stops, day)
    assert again == taken
    # Each fill read was observed before the clock, of a game started by it.
    assert at > pf.START and found["fill_utc"].max() < at  # type: ignore[operator]
