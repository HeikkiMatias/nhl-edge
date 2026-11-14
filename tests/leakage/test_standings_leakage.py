"""Point-in-time rules for the standings by date (#422). A date's standings include that date's
games, so they count as public at 10:00 UTC the morning after it, with its results (ADR 0003).
A prediction reads only standings observed strictly before it, so a game on date D at E2's
prediction time (10:00 US Eastern on D, plus a second) reads those of D-1 and never its own
date's, on either side of a daylight saving change."""

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import polars as pl

from nhl_edge.backtest.market import PREDICTION_LAG
from nhl_edge.ingest.games import result_public_utc
from nhl_edge.ingest.sbr import ET, OPEN_ASSUMED_AT_ET
from nhl_edge.ingest.standings import parse_standings, standings_as_of

FIXTURE = Path(__file__).parents[1] / "unit" / "fixtures" / "nhl_api" / "standings_2014-04-08.json"
TEAMS = ("BOS", "MTL", "TOR", "CHI", "LAK")
# US daylight saving time ended on 2013-11-03 and began on 2014-03-09.
FALL_BACK = date(2013, 11, 3)
SPRING_FORWARD = date(2014, 3, 9)


def days_around(middle: date, before: int = 5, after: int = 5) -> list[date]:
    return [middle + timedelta(days=n) for n in range(-before, after + 1)]


def standings(days: list[date]) -> pl.DataFrame:
    """The fixture's lines relabelled as each date's: synthetic, as only the dates matter here."""
    frames = []
    for day in days:
        data = json.loads(FIXTURE.read_bytes())
        for entry in data["standings"]:
            entry["date"] = day.isoformat()
        raw_key = f"nhl/standings/{day.isoformat()}/20261114T120000Z"
        frames.append(parse_standings(json.dumps(data).encode(), day, raw_key))
    return pl.concat(frames)


def e2_prediction_utc(day: date) -> datetime:
    """E2's prediction time of a game on day: a second after its opener's assumed time, 10:00 US
    Eastern on the game date (sbr.open_assumed_utc, backtest.market)."""
    return datetime.combine(day, OPEN_ASSUMED_AT_ET, tzinfo=ET).astimezone(UTC) + PREDICTION_LAG


def asks(moments: list[datetime]) -> pl.DataFrame:
    return pl.DataFrame(
        [(team, moment) for team in TEAMS for moment in moments],
        schema={"team": pl.String, "as_of_utc": pl.Datetime("us", "UTC")},
        orient="row",
    )


def test_every_row_read_is_observed_strictly_before_its_ask() -> None:
    days = days_around(SPRING_FORWARD) + days_around(FALL_BACK)
    frame = standings(days)
    # Every hour over both windows, and each row's own observed time and a microsecond around it.
    start = datetime.combine(min(days), time(0), UTC)
    hours = [start + timedelta(hours=n) for n in range(24 * 160)]
    edges = [
        moment + shift
        for moment in frame["observed_utc"].unique().to_list()
        for shift in (timedelta(microseconds=-1), timedelta(0), timedelta(microseconds=1))
    ]
    read = standings_as_of(frame, asks(hours + edges))
    found = read.filter(pl.col("observed_utc").is_not_null())
    assert found.height > 0
    assert (found["observed_utc"] < found["as_of_utc"]).all()
    # The latest such row, by brute force on a sample.
    for row in read.gather_every(97).iter_rows(named=True):
        known = frame.filter(
            pl.col("team") == row["team"], pl.col("observed_utc") < row["as_of_utc"]
        )
        latest = known["observed_utc"].max() if known.height else None
        assert row["observed_utc"] == latest


def test_a_game_never_reads_its_own_date_s_standings_at_e2() -> None:
    for middle in (FALL_BACK, SPRING_FORWARD):
        days = days_around(middle)
        frame = standings(days)
        for day in days[1:]:
            read = standings_as_of(frame, asks([e2_prediction_utc(day)]))
            assert read["standings_date"].to_list() == [day - timedelta(days=1)] * len(TEAMS)
            assert (read["observed_utc"] < read["as_of_utc"]).all()


def test_a_date_s_standings_are_public_only_after_every_game_of_the_date() -> None:
    # Observed with the date's results (games.observed_utc), after the last minute of the date in
    # US Eastern time, so after the start of any game of the date; and still before E2's
    # prediction of the next date's games, which read them. On both sides of a clock change.
    for day in days_around(FALL_BACK) + days_around(SPRING_FORWARD):
        observed = standings([day])["observed_utc"].unique().to_list()
        assert observed == [result_public_utc(day)]
        assert observed[0] > datetime.combine(day, time(23, 59), tzinfo=ET).astimezone(UTC)
        assert observed[0] < e2_prediction_utc(day + timedelta(days=1))
