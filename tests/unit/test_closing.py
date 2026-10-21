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
    assert proxy_of(quotes(pair(MIDDAY), pair(PRE7, age=5 * MINUTE))) == PRE7
    # A pair with one side is no quote.
    assert proxy_of(quotes(pair(MIDDAY), pair(PRE7, sides=("home",)))) is None
    # More than 90 minutes before the start is too early; exactly 90 is not.
    assert proxy_of(quotes(pair(MIDDAY), pair(START - 91 * MINUTE))) is None
    assert proxy_of(quotes(pair(MIDDAY), pair(START - 90 * MINUTE))) == START - 90 * MINUTE
    # Each book has its own proxy.
    other = pair(later, book="bet365")
    found = quotes(pair(MIDDAY), pair(PRE7), other)
    assert (proxy_of(found), proxy_of(found, "bet365")) == (PRE7, later)
    # A day without a decision snapshot made no decision, and has no close (Codex on #189).
    assert proxy_of(quotes(pair(PRE7))) is None


def test_the_decision_snapshot_is_never_a_close() -> None:
    # A 13:00 ET matinee: the midday snapshot falls in its last 90 minutes, but it is the day's
    # decision snapshot, the price bet.
    matinee = datetime(2026, 10, 10, 17, tzinfo=UTC)
    midday = datetime(2026, 10, 10, 16, 45, 30, tzinfo=UTC)
    after = matinee + timedelta(hours=1)
    assert proxy_of(quotes(pair(midday, matinee)), now=after) is None
    # A second run in the window (a retry after a failed decision, Codex's P0 on #189) may have
    # been the one decided on: no snapshot in the window is a close.
    again = datetime(2026, 10, 10, 16, 58, tzinfo=UTC)  # 12:58 ET
    assert proxy_of(quotes(pair(midday, matinee), pair(again, matinee)), now=after) is None
    later = datetime(2026, 10, 10, 16, 59, 30, tzinfo=UTC)  # 12:59:30 ET, still in the window
    closes = quotes(pair(midday, matinee), pair(again, matinee), pair(later, matinee))
    assert proxy_of(closes, now=after) is None
    # The decision snapshot is the day's last in the window, whatever book or game it priced.
    assert closing.decision_snapshots(quotes(pair(again, matinee), pair(midday, START)))[
        "decision_utc"
    ].to_list() == [again]


def test_only_started_games_are_marked() -> None:
    frame = quotes(pair(MIDDAY), pair(PRE7))
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
    # A lone fresh side in the span is no quote: missing, not stale (Codex on #189).
    lone_start = START + timedelta(days=4)
    day = timedelta(days=1)
    # Codex on #189: a game Pinnacle never priced is missing, not left out; and a 19:00 start at
    # the decision moved to 21:30 later stays eligible: missing, never "no pre-game snapshot".
    unpriced_start = START + 5 * day
    moved_start = START + 6 * day + timedelta(hours=2, minutes=30)
    late_moved = moved_start + day
    frame = quotes(
        pair(MIDDAY + 5 * day, unpriced_start, book="bet365", event="unpriced"),
        pair(MIDDAY + 5 * day, unpriced_start, event="other"),
        pair(unpriced_start - 15 * MINUTE, unpriced_start, event="other"),
        pair(MIDDAY + 6 * day, START + 6 * day, event="moved"),
        pair(PRE7 + 6 * day + timedelta(hours=3), moved_start, event="moved", book="bet365"),
        pair(MIDDAY, event="proxy"),
        pair(PRE7, event="proxy"),
        pair(MIDDAY + day, stale_start, event="stale"),
        pair(stale_start - 15 * MINUTE, stale_start, age=20 * MINUTE, event="stale"),
        pair(MIDDAY + 2 * day, missing_start, event="missing"),
        pair(datetime(2026, 10, 10, 16, 45, 30, tzinfo=UTC), matinee, event="matinee"),
        pair(late - 6 * timedelta(hours=1), late, event="late"),
        # A delayed run 75 minutes before a 21:30 start left a fresh pair: still no pre-game
        # slot was due, so the game stays outside the floor (Codex on #189).
        pair(late - 75 * MINUTE, late, event="late"),
        # The decision snapshot priced only spreads for this 19:00 game, then h2h came back
        # after a move to 21:30: eligible from the start the decision showed (Codex's P0 on #189).
        pair(MIDDAY + 7 * day, START + 7 * day, event="spreads", market="spreads"),
        pair(PRE7 + 7 * day + timedelta(hours=3), late_moved, event="spreads", book="bet365"),
        # Pinnacle pulled its line at the decision snapshot, which another book priced, and came
        # back before the start: the day's decision snapshot still stands (Codex on #189).
        pair(MIDDAY + 8 * day, START + 8 * day, event="pulled", book="bet365"),
        pair(PRE7 + 8 * day, START + 8 * day, event="pulled"),
        pair(MIDDAY + 4 * day, lone_start, event="lone"),
        pair(lone_start - 15 * MINUTE, lone_start, event="lone", sides=("home",)),
    ).with_columns(game_id=pl.lit(None, pl.Int64))
    closes = closing.pinnacle_closes(frame, NOW + timedelta(days=9))
    status = dict(closes.select("event_id", "status").iter_rows())
    assert status == {
        "proxy": closing.PROXY,
        "stale": closing.STALE,
        "missing": closing.MISSING,
        "matinee": closing.NO_PREGAME,
        "late": closing.NO_PREGAME,
        "lone": closing.MISSING,
        "unpriced": closing.MISSING,
        "other": closing.PROXY,
        "moved": closing.MISSING,
        "spreads": closing.MISSING,
        "pulled": closing.PROXY,
    }
    lead = closes.filter(pl.col("event_id") == "proxy")["lead"].item()
    assert lead == START - PRE7
    # The late game's pair is still its flagged closing proxy, though it counts outside the floor.
    assert closes.filter(pl.col("event_id") == "late")["proxy_utc"].item() == late - 75 * MINUTE


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
    # The new start counts in any market: a snapshot with totals only still moves the game, so
    # the old pair is no close (Codex on #189).
    totals = quotes(
        pair(MIDDAY), pair(PRE7), pair(later - 2 * timedelta(hours=1), later, market="totals")
    )
    assert closing.proxies(totals, START + timedelta(hours=3)).is_empty()
    assert proxy_of(totals.filter(pl.col("market") == "h2h"), now=START + timedelta(hours=3))


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
