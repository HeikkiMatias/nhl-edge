"""Point-in-time rules for Daily Faceoff's line combinations (#121): a prediction may use only
pages fetched before it, whatever times Daily Faceoff gives its lines and news."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from nhl_edge.ingest.dailyfaceoff import parse_line_combinations
from nhl_edge.lake.tables import known_at

PAGE = (
    Path(__file__).parents[1]
    / "unit"
    / "fixtures"
    / "dailyfaceoff"
    / "line-combinations_WSH_2026-10-08_trimmed.html"
).read_bytes()
FETCHED = datetime(2026, 10, 8, 10, 1, 10, tzinfo=UTC)


def test_rows_count_from_the_fetch_not_the_page_times() -> None:
    frame = parse_line_combinations(PAGE, FETCHED, "k", date(2026, 10, 8))
    # The lines were updated two days before, and Roy's news the day before, but this copy was
    # only seen at FETCHED.
    earliest = frame["lines_updated_utc"].min()
    assert isinstance(earliest, datetime) and earliest < FETCHED
    assert known_at(frame, earliest + timedelta(minutes=1)).is_empty()
    assert known_at(frame, FETCHED).is_empty()
    assert known_at(frame, FETCHED + timedelta(seconds=1)).height == frame.height


def test_no_page_time_follows_its_fetch() -> None:
    frame = parse_line_combinations(PAGE, FETCHED, "k", date(2026, 10, 8))
    assert (frame["lines_updated_utc"] <= frame["observed_utc"]).all()
    assert (frame["news_utc"].drop_nulls() <= FETCHED).all()
