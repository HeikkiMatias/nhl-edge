"""Point-in-time rules for kalshi_lines (#319, hard rule 1): a market of a game of the slate is
never kept at or after its start, one on the slate's dates the slate lacks never, since its start
isn't known, and each row is dated by its own response's fetch, never the poll's start. On
synthetic responses only: the repository holds no Kalshi response."""

import json
from datetime import timedelta
from pathlib import Path

import kalshi_fixtures as kf
import polars as pl
import predict_fixtures as pf
from kalshi_watch_fixtures import START, Fake, top

from nhl_edge.ingest import kalshi_watch as kw
from nhl_edge.lake.raw import RawStore

GAME = {
    "game_id": 2026020053,
    "season": 20262027,
    "game_date": pf.DAY.isoformat(),
    "start_utc": pf.START.isoformat(),
    "away": "PIT",
    "home": "WSH",
}
TOTAL = top("KXNHLTOTAL-26OCT07PITWSH-6")


def listing(*markets: dict[str, object]) -> bytes:
    return json.dumps({"markets": list(markets)}).encode()


def test_a_slate_games_market_is_never_kept_at_or_after_its_start() -> None:
    before = pf.START - timedelta(seconds=1)
    kept = kw.parse_lines(listing(TOTAL), kw.LINES, [GAME], [pf.DAY], before, "k")
    assert kept.height == 1
    for at in (pf.START, pf.START + timedelta(hours=1)):
        assert kw.parse_lines(listing(TOTAL), kw.LINES, [GAME], [pf.DAY], at, "k").is_empty()


def test_a_game_on_the_slates_dates_that_the_slate_lacks_is_never_kept() -> None:
    # Its start isn't known, so it may be under way: the listing holds in-play markets too.
    rows = kw.parse_lines(listing(TOTAL), kw.LISTING, [], [pf.DAY], START, "k")
    assert rows.is_empty()
    # The next day's game, on no date the slate covers, has not started.
    later = top("KXNHLTOTAL-26OCT08PITWSH-6")
    assert kw.parse_lines(listing(later), kw.LISTING, [], [pf.DAY], START, "k").height == 1


def test_each_row_is_dated_by_its_own_fetch(tmp_path: Path) -> None:
    # Requests 5 seconds apart: a poll's responses arrive after its start, each at its own time.
    clock = kf.Clock(START)
    served = Fake(clock)
    store = RawStore(tmp_path / "raw")
    client = kf.make(store, served, clock, min_interval_s=5.0)
    kw.watch(
        client=client,
        slate=pf.slate,
        until=START + timedelta(minutes=10),
        echo=print,
        lines=True,
    )
    asked = sorted({at for at, _ in served.of(kw.LISTING)})
    table, _ = kw.stored_lines(store)
    listed = table.filter(pl.col("kind") == kw.LISTING)
    # Each series' rows carry the time its own request was answered, after the poll began.
    assert listed.height and set(listed["observed_utc"]) == set(asked) and len(asked) > 1
    assert (listed["observed_utc"] > START).all()
