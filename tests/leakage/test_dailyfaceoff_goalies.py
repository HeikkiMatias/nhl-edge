"""Point-in-time rules for Daily Faceoff starting goalies: a prediction may use only pages
fetched before it, whatever time Daily Faceoff gives its report, and never a page fetched after
the start."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from nhl_edge.ingest.dailyfaceoff import parse_starting_goalies
from nhl_edge.lake.tables import known_at

PAGE = (
    Path(__file__).parents[1]
    / "unit"
    / "fixtures"
    / "dailyfaceoff"
    / "starting-goalies_2026-09-29_trimmed.html"
).read_bytes()
FETCHED = datetime(2026, 9, 29, 12, 10, tzinfo=UTC)
CAR_START = datetime(2026, 9, 29, 21, 0, tzinfo=UTC)


def test_rows_count_from_the_fetch_not_the_report_time() -> None:
    frame = parse_starting_goalies(PAGE, FETCHED, "k")
    confirmed = frame.filter(pl.col("status") == "Confirmed")
    # Jarry's report is dated the day before, but this copy was only seen at FETCHED.
    reported = confirmed["reported_utc"].item()
    assert reported < FETCHED
    assert known_at(frame, reported + timedelta(minutes=1)).is_empty()
    assert known_at(frame, FETCHED).is_empty()
    assert known_at(frame, FETCHED + timedelta(seconds=1)).height == frame.height


def test_no_row_is_observed_at_or_after_the_start() -> None:
    late = parse_starting_goalies(PAGE, CAR_START + timedelta(minutes=5), "k")
    assert "CAR" not in set(late["team"])
    assert (late["observed_utc"] < late["start_utc"]).all()
