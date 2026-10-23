"""A fake Kalshi for #319's watch tests, answering the listing, the books and the trades with
synthetic responses in the documented shapes: every number is made up, since the public
repository holds no Kalshi response (Developer Agreement 3.1)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import kalshi_fixtures as kf
import polars as pl
import predict_fixtures as pf

from nhl_edge.ingest import kalshi_watch as kw
from nhl_edge.lake.raw import RawStore

# 12:55 ET on the fixture day: the three games start at 19:30 and 22:00 ET.
START = datetime(2026, 10, 7, 16, 55, tzinfo=UTC)
TRADE_AT = datetime(2026, 10, 7, 16, 50, tzinfo=UTC)


def top(ticker: str) -> dict[str, Any]:
    """SYNTHETIC: a made-up top of book in the listing's documented shape."""
    return {
        "ticker": ticker,
        "status": "active",
        "yes_bid_dollars": "0.4400",
        "yes_bid_size_fp": "120.00",
        "yes_ask_dollars": "0.4600",
        "yes_ask_size_fp": "60.00",
        "last_price_dollars": "0.4500",
        "volume_fp": "1000.00",
        "open_interest_fp": "500.00",
    }


def trade(ticker: str, n: int = 1, at: datetime = TRADE_AT) -> dict[str, Any]:
    """SYNTHETIC: a made-up trade in the documented shape."""
    return {
        "trade_id": f"{ticker}-{n}",
        "ticker": ticker,
        "count_fp": "3.00",
        "yes_price_dollars": "0.4500",
        "no_price_dollars": "0.5500",
        "taker_outcome_side": "yes",
        "created_time": at.isoformat().replace("+00:00", "Z"),
        "is_block_trade": False,
    }


class Fake:
    """A fake Kalshi answering the three endpoints, with every request kept beside the clock's
    time at it."""

    def __init__(self, clock: kf.Clock, pages: int = 1, missing: frozenset[str] = frozenset()):
        self.clock, self.pages, self.missing = clock, pages, missing
        self.requests: list[tuple[datetime, httpx.Request]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((self.clock.now(), request))
        path, params = request.url.path, request.url.params
        if path.endswith("/markets"):
            tickers = params["tickers"].split(",")
            return httpx.Response(200, json={"markets": [top(t) for t in tickers]})
        if path.endswith("/orderbook"):
            ticker = path.split("/")[-2]
            if ticker in self.missing:
                return httpx.Response(404, json={"code": "not_found"})
            return httpx.Response(200, content=kf.BOOK)
        if path.endswith("/markets/trades"):
            ticker, cursor = params["ticker"], params.get("cursor")
            page = int(cursor) if cursor else 1
            following = str(page + 1) if page < self.pages else ""
            body = {"trades": [trade(ticker, page)], "cursor": following}
            return httpx.Response(200, json=body)
        raise AssertionError(f"unexpected request {request.url}")

    def of(self, kind: str) -> list[tuple[datetime, httpx.Request]]:
        ends = {kw.TOPS: "/markets", kw.BOOKS: "/orderbook", kw.TRADES: "/markets/trades"}
        return [(at, r) for at, r in self.requests if r.url.path.endswith(ends[kind])]


def run(
    tmp_path: Path,
    minutes: float,
    *,
    fake: Callable[[kf.Clock], Fake] = Fake,
    games: pl.DataFrame | None = None,
    start: datetime = START,
) -> tuple[kw.WatchReport, Fake, RawStore, list[str]]:
    clock = kf.Clock(start)
    served = fake(clock)
    store = RawStore(tmp_path / "raw")
    lines: list[str] = []
    slate = pf.slate() if games is None else games
    report = kw.watch(
        client=kf.make(store, served, clock),
        slate=lambda: slate,
        until=start + timedelta(minutes=minutes),
        echo=lines.append,
    )
    return report, served, store, lines
