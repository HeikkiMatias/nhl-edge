"""The closing proxy (#21, ADR 0033): per started game, book and market, the latest fresh pair
after the day's decision snapshot, before the start and at most 90 minutes before it; and why
Pinnacle has none when it hasn't."""

from datetime import UTC, datetime, timedelta

import polars as pl
from closing_fixtures import MIDDAY, MINUTE, NOW, PRE7, START, pair, quotes

from nhl_edge.ingest.odds import ODDS_FRAME_SCHEMA
from nhl_edge.market import closing


def proxy_of(frame: pl.DataFrame, book: str = "pinnacle", now: datetime = NOW) -> datetime | None:
    found = closing.proxies(frame, now).filter(pl.col("book") == book)
    return found["snapshot_utc"].item() if found.height else None


def test_the_proxy_is_the_latest_fresh_pair_in_the_last_90_minutes() -> None:
    later = START - 5 * MINUTE
    assert proxy_of(quotes(pair(MIDDAY), pair(PRE7), pair(later))) == later
    # A stale latest pair is skipped, and the earlier fresh one stands in.
    stale = pair(later, age=6 * MINUTE)
    assert proxy_of(quotes(pair(MIDDAY), pair(PRE7), stale)) == PRE7
    # Exactly 5 minutes old still counts.
    assert proxy_of(quotes(pair(PRE7, age=5 * MINUTE))) == PRE7
    # A pair with one side is no quote.
    assert proxy_of(quotes(pair(PRE7, sides=("home",)))) is None
    # More than 90 minutes before the start is too early; exactly 90 is not.
    assert proxy_of(quotes(pair(START - 91 * MINUTE))) is None
    assert proxy_of(quotes(pair(START - 90 * MINUTE))) == START - 90 * MINUTE
    # Each book has its own proxy.
    other = pair(later, book="bet365")
    found = quotes(pair(PRE7), other)
    assert (proxy_of(found), proxy_of(found, "bet365")) == (PRE7, later)


def test_the_decision_snapshot_is_never_a_close() -> None:
    # A 13:00 ET matinee: the midday snapshot falls in its last 90 minutes, but it is the day's
    # decision snapshot, the price bet. A later snapshot in the window can be its close.
    matinee = datetime(2026, 10, 10, 17, tzinfo=UTC)
    midday = datetime(2026, 10, 10, 16, 45, 30, tzinfo=UTC)
    after = matinee + timedelta(hours=1)
    assert proxy_of(quotes(pair(midday, matinee)), now=after) is None
    again = datetime(2026, 10, 10, 16, 58, tzinfo=UTC)  # 12:58 ET, a second run in the window
    assert proxy_of(quotes(pair(midday, matinee), pair(again, matinee)), now=after) == again
    # The decision snapshot is the day's, whatever book or game it priced.
    assert closing.decision_snapshots(quotes(pair(again, matinee), pair(midday, START)))[
        "decision_utc"
    ].to_list() == [midday]


def test_only_started_games_are_marked() -> None:
    frame = quotes(pair(PRE7))
    assert closing.proxies(frame, START - MINUTE).is_empty()
    assert closing.proxies(frame, START).height == 1
    flagged = closing.flag(quotes(pair(MIDDAY), pair(PRE7)), NOW)
    assert flagged.filter("is_closing_proxy")["snapshot_utc"].to_list() == [PRE7, PRE7]
    assert flagged.columns == list(ODDS_FRAME_SCHEMA)


def test_without_a_proxy_pinnacle_says_why() -> None:
    # 19:00 ET with a fresh pre7 pair; a stale one only; none at all.
    stale_start = START + timedelta(days=1)
    missing_start = START + timedelta(days=2)
    # 13:00 ET and 21:30 ET: no pre-game slot within 90 minutes of either start.
    matinee = datetime(2026, 10, 10, 17, tzinfo=UTC)
    late = datetime(2026, 10, 11, 1, 30, tzinfo=UTC)
    frame = quotes(
        pair(PRE7, event="proxy"),
        pair(stale_start - 15 * MINUTE, stale_start, age=20 * MINUTE, event="stale"),
        pair(missing_start - 6 * timedelta(hours=1), missing_start, event="missing"),
        pair(datetime(2026, 10, 10, 16, 45, 30, tzinfo=UTC), matinee, event="matinee"),
        pair(late - 6 * timedelta(hours=1), late, event="late"),
    ).with_columns(game_id=pl.lit(None, pl.Int64))
    closes = closing.pinnacle_closes(frame, NOW + timedelta(days=4))
    status = dict(closes.select("event_id", "status").iter_rows())
    assert status == {
        "proxy": closing.PROXY,
        "stale": closing.STALE,
        "missing": closing.MISSING,
        "matinee": closing.NO_PREGAME,
        "late": closing.NO_PREGAME,
    }
    lead = closes.filter(pl.col("event_id") == "proxy")["lead"].item()
    assert lead == START - PRE7


def test_a_start_moved_between_snapshots_is_one_game_at_its_latest_start() -> None:
    # The Odds API moved the start ten minutes after the midday snapshot: one game, starting
    # when its latest snapshot says, and its pre7 quote still its close.
    moved = START + 10 * MINUTE
    frame = quotes(pair(MIDDAY, START), pair(PRE7, moved)).with_columns(
        game_id=pl.lit(2026020053, pl.Int64)
    )
    closes = closing.pinnacle_closes(frame, NOW)
    assert closes.select("start_utc", "proxy_utc", "status").rows() == [
        (moved, PRE7, closing.PROXY)
    ]
    # A game postponed to the next day keeps its event: the quotes before it are no close.
    later = START + timedelta(days=1)
    postponed = quotes(pair(PRE7, START), pair(later - 2 * timedelta(hours=1), later))
    assert closing.proxies(postponed, later + timedelta(hours=3)).is_empty()


def test_a_pregame_slot_is_scheduled_within_90_minutes_of_evening_starts() -> None:
    starts = pl.DataFrame(
        {
            "start_utc": [
                datetime(2026, 10, 7, 23, tzinfo=UTC),  # 19:00 EDT: pre7 at 18:45
                datetime(2026, 10, 7, 17, tzinfo=UTC),  # 13:00 EDT
                datetime(2026, 10, 8, 1, 30, tzinfo=UTC),  # 21:30 EDT: pre8 is 105 min before
                datetime(2026, 10, 8, 2, tzinfo=UTC),  # 22:00 EDT: pre10 at 21:45
                datetime(2026, 12, 8, 1, tzinfo=UTC),  # 20:00 EST: pre7 and pre8
            ]
        },
        schema={"start_utc": pl.Datetime("us", "UTC")},
    )
    assert closing.scheduled_pregame(starts).to_list() == [True, False, False, True, True]
