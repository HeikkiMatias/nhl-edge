"""Point-in-time rules for #308's edge bids (live/shadow.py): a bid fills only from a Pinnacle quote
taken after the decision's publication, when it would rest from, and before the start, never one
known at the decision itself or once the game is under way, and the starter it compares reads only
the polls observed before each moment: the decision's input cutoff, its snapshot
(predict.input_cutoff), and the fill's."""

from datetime import UTC, datetime, timedelta

import polars as pl
import predict_fixtures as pf

from nhl_edge.lake.schemas import DailyFaceoffGoalies, dtypes
from nhl_edge.live import predict as lp
from nhl_edge.live import shadow

WSH = 2026020053


def bids() -> pl.DataFrame:
    """The fixture day's bids: the policy bet every home side, so the away sides."""
    inputs = pf.day()
    return shadow.edge_bids(lp.ledger(lp.decide(inputs), inputs))


def fill_of(*snapshots: tuple[datetime, float]) -> datetime | None:
    """WSH's away bid's fill time from Pinnacle quotes (snapshot, away price), fresh at each."""
    quotes = pf.quotes(*(pf.quote(at, WSH, "pinnacle", 1.5, away) for at, away in snapshots))
    odds = quotes.with_columns(game_id=pl.col("event_id").str.strip_prefix("e").cast(pl.Int64))
    fills = shadow.edge_fills(bids(), odds)
    return fills.filter(pl.col("game_id") == WSH)["fill_utc"].item()


def test_a_quote_before_the_bid_rests_or_once_the_game_started_fills_nothing() -> None:
    wsh = bids().filter(pl.col("game_id") == WSH)
    decision, published = wsh["decision_snapshot_utc"].item(), wsh["published_utc"].item()
    assert decision < published
    assert fill_of((decision, 9.0)) is None
    assert fill_of((decision - timedelta(minutes=1), 9.0)) is None
    # Between the snapshot and the publication the bid doesn't rest yet (Codex on #313).
    assert fill_of((decision + (published - decision) / 2, 9.0)) is None
    assert fill_of((published, 9.0)) is None
    assert fill_of((pf.START, 9.0), (pf.START + timedelta(minutes=5), 9.0)) is None
    later = published + timedelta(seconds=1)
    assert fill_of((published, 9.0), (later, 9.0)) == later


def poll(observed: datetime, name: str) -> dict[str, object]:
    return {
        "season": 20262027,
        "game_date": pf.DAY,
        "start_utc": pf.START,
        "team": "PIT",
        "is_home": False,
        "goalie_name": name,
        "dfo_goalie_id": None,
        "status": "Likely",
        "reported_utc": None,
        "source_url": None,
        "observed_utc": observed,
        "raw_key": "k",
    }


def test_the_starter_compared_is_the_one_polled_before_each_moment() -> None:
    # PIT's goalie changes in a poll observed exactly at the fill: it was not known before the
    # fill, so the fill shows no change; a second earlier changes it.
    fill = datetime(2026, 10, 7, 22, 0, tzinfo=UTC)
    quotes = pf.quotes(pf.quote(fill, WSH, "pinnacle", 1.5, 9.0, slot="pre7"))
    odds = quotes.with_columns(game_id=pl.col("event_id").str.strip_prefix("e").cast(pl.Int64))
    fills = shadow.edge_fills(bids(), odds).filter(pl.col("fill_utc").is_not_null())
    decision = fills["decision_snapshot_utc"].item()
    before = poll(decision - timedelta(hours=1), "Tristan Jarry")
    schema = dtypes(DailyFaceoffGoalies)
    for changed_at, expected in ((fill, False), (fill - timedelta(seconds=1), True)):
        polls = pl.DataFrame([before, poll(changed_at, "Alex Nedeljkovic")], schema=schema)
        assert shadow.starter_changes(fills, polls)["starter_changed"].item() is expected
    # A poll observed exactly at the decision is not the decision's.
    at_decision = pl.DataFrame([poll(decision, "Tristan Jarry")], schema=schema)
    assert shadow.starter_changes(fills, at_decision)["starter_changed"].item() is None
    # Nor is one between the snapshot and the decision's own clock (Codex on #313): the inputs
    # all precede the snapshot, so a change then is news the decision didn't have.
    between = decision + (fills["prediction_utc"].item() - decision) / 2
    late = pl.DataFrame([before, poll(between, "Alex Nedeljkovic")], schema=schema)
    assert shadow.starter_changes(fills, late)["starter_changed"].item() is True
