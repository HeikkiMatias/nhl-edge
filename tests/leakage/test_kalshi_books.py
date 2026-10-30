"""Point-in-time rules for Kalshi's order books (#282), on synthetic books: an order placed at a
moment may see only the books fetched strictly before it, the poll never asks for a game at or
after its start, and no model or feature code reads them (they are never a model input)."""

from datetime import datetime, timedelta
from pathlib import Path

import httpx
import kalshi_fixtures as kf
import pandera.errors
import polars as pl
import pytest
import real_fixtures as rf

from nhl_edge.ingest.kalshi import books_before, markets, parse_orderbook, run_books
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import KalshiBooks

MORNING = kf.FETCHED
PRE_GAME = kf.START - timedelta(minutes=40)


def books() -> pl.DataFrame:
    return pl.concat(
        [
            parse_orderbook(kf.BOOK, kf.MARKET, MORNING, "morning"),
            parse_orderbook(kf.book([["0.4500", "3.00"]], []), kf.MARKET, PRE_GAME, "pre"),
        ]
    )


def test_an_order_sees_only_books_observed_before_it() -> None:
    every = books()
    assert books_before(every, MORNING).is_empty()
    at_pre_game = books_before(every, PRE_GAME)
    assert set(at_pre_game["raw_key"]) == {"morning"}
    assert (at_pre_game["observed_utc"] < PRE_GAME).all()
    after = books_before(every, PRE_GAME + timedelta(microseconds=1))
    assert after.height == every.height


def test_no_row_is_observed_at_or_after_the_start() -> None:
    assert parse_orderbook(kf.BOOK, kf.MARKET, kf.START, "live").is_empty()
    late = books().with_columns(observed_utc=pl.col("start_utc"))
    with pytest.raises(pandera.errors.SchemaError):
        KalshiBooks.validate(late)


def test_the_poll_never_asks_for_a_game_at_or_after_its_start(tmp_path: Path) -> None:
    # The poll starts 0.3 seconds before two of the three alerted games, 0.25 seconds between
    # requests: those two games start while it runs. Every request is timed against the start
    # of the game it asks for.
    clock = kf.Clock(kf.START - timedelta(seconds=0.3))
    starts = {market.ticker: market.start_utc for market in markets(rf.alert_rows(), rf.ALERT)}
    asked: list[tuple[str, datetime]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append((request.url.path.split("/")[-2], clock.now()))
        return httpx.Response(200, content=kf.BOOK)

    report = run_books(
        kalshi=kf.make(RawStore(tmp_path), handler, clock, min_interval_s=0.25),
        alerts=rf.alert_rows(),
        slot="pre7",
        echo=lambda _: None,
    )
    assert [ticker for ticker, _ in asked] == [
        "KXNHLGAME-26OCT07PITWSH-PIT",
        "KXNHLGAME-26OCT07PITWSH-WSH",
        "KXNHLGAME-26OCT07EDMANA-EDM",
        "KXNHLGAME-26OCT07EDMANA-ANA",
    ]
    assert report.started == 2
    assert all(at < starts[ticker] for ticker, at in asked)
    # Once all have started, nothing is asked at all.
    clock.advance(timedelta(hours=3))
    asked.clear()
    run_books(
        kalshi=kf.make(RawStore(tmp_path), handler, clock),
        alerts=rf.alert_rows(),
        slot="pre10",
        echo=lambda _: None,
    )
    assert asked == []


def test_no_model_or_feature_code_reads_the_books() -> None:
    # Kalshi's data is for the owner's own orders only (#281): only its ingest, its table's
    # registration and schema, the trading client (#286), the orders and the real record built
    # on it (ADR 0040), the orders' fills valued against the close (#318), the watch of every
    # game's markets (#319), the re-valuation of the resting bids it triggers (#321), the shadow
    # that selects on Kalshi's own prices (#320, ADR 0046), the scale-up reading of the real fills
    # (#322, ADR 0047), the paper quotes on both teams' markets (#358) and the CLI that runs them
    # name it, and only the first three and the CLI name the books' table.
    src = Path(__file__).parents[2] / "src" / "nhl_edge"
    texts = {path.relative_to(src).as_posix(): path.read_text() for path in src.rglob("*.py")}
    naming = {path for path, text in texts.items() if "kalshi" in text.lower()}
    assert naming == {
        "cli.py",
        "ingest/kalshi.py",
        "ingest/kalshi_watch.py",
        "lake/schemas.py",
        "lake/tables.py",
        "live/exchange.py",
        "live/fills.py",
        "live/kalshi_quotes.py",
        "live/kalshi_shadow.py",
        "live/kalshi_trade.py",
        "live/placement.py",
        "live/real.py",
        "live/real_ledger.py",
        "live/revalue.py",
        "live/scale_up.py",
    }
    books = {
        path for path, text in texts.items() if "kalshi_books" in text or "KalshiBooks" in text
    }
    assert books <= {"cli.py", "ingest/kalshi.py", "lake/schemas.py", "lake/tables.py"}
