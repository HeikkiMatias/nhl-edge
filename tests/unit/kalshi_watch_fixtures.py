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

from nhl_edge.ingest import kalshi
from nhl_edge.ingest import kalshi_watch as kw
from nhl_edge.lake.raw import RawStore

# 12:55 ET on the fixture day: the three games start at 19:30 and 22:00 ET.
START = datetime(2026, 10, 7, 16, 55, tzinfo=UTC)
TRADE_AT = datetime(2026, 10, 7, 16, 50, tzinfo=UTC)


def top(ticker: str) -> dict[str, Any]:
    """SYNTHETIC: a made-up top of book in the listing's documented shape."""
    return {
        "ticker": ticker,
        "event_ticker": "-".join(ticker.split("-")[:2]),
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


def listed(series: str, games: pl.DataFrame) -> list[dict[str, Any]]:
    """SYNTHETIC: one made-up market of each game of the slate in a series, as a listing gives
    it: the winner's two teams, a total's line, a spread's and a team total's home team, and a
    prop's home player at a threshold of 2."""
    markets = []
    for row in games.iter_rows(named=True):
        event = f"{series}-{kalshi.event_key(row['game_date'], row['away'], row['home'])}"
        home = kalshi.kalshi_code(row["home"])
        suffixes = {
            kalshi.SERIES: [kalshi.kalshi_code(row["away"]), home],
            "KXNHLTOTAL": ["6"],
            "KXNHLSPREAD": [f"{home}2"],
            "KXNHLTEAMTOTAL": [f"{home}3"],
        }.get(series, [f"{home}PLAYER1-2"])
        for suffix in suffixes:
            markets.append(
                top(f"{event}-{suffix}")
                | {"yes_sub_title": f"line {suffix}", "strike_type": "greater", "floor_strike": 5.5}
            )
    return markets


class Fake:
    """A fake Kalshi answering the three endpoints and the series listings, with every request
    kept beside the clock's time at it. A listing holds a made-up market of each of games."""

    def __init__(
        self,
        clock: kf.Clock,
        pages: int = 1,
        missing: frozenset[str] = frozenset(),
        games: pl.DataFrame | None = None,
    ):
        self.clock, self.pages, self.missing = clock, pages, missing
        self.games = pf.slate() if games is None else games
        self.requests: list[tuple[datetime, httpx.Request]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((self.clock.now(), request))
        path, params = request.url.path, request.url.params
        if path.endswith("/markets") and "series_ticker" in params:
            markets = listed(params["series_ticker"], self.games)
            return httpx.Response(200, json={"markets": markets, "cursor": ""})
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
        """The requests of one kind: a watch kind, or a line kind (kw.LINE_KINDS)."""
        if kind in kw.LINE_KINDS:
            series = {kw.LISTING: kw.GAME_SERIES, kw.PROPS: kw.PROP_SERIES}.get(kind, ())

            def asked(params: httpx.QueryParams) -> bool:
                if series:
                    return params.get("series_ticker") in series
                return not params.get("tickers", kalshi.SERIES).startswith(kalshi.SERIES)

            return [
                (at, r)
                for at, r in self.requests
                if r.url.path.endswith("/markets") and asked(r.url.params)
            ]
        ends = {kw.TOPS: "/markets", kw.BOOKS: "/orderbook", kw.TRADES: "/markets/trades"}
        return [
            (at, r)
            for at, r in self.requests
            if r.url.path.endswith(ends[kind])
            and (kind != kw.TOPS or r.url.params.get("tickers", "").startswith(kalshi.SERIES))
        ]


def run(
    tmp_path: Path,
    minutes: float,
    *,
    fake: Callable[[kf.Clock], Fake] = Fake,
    games: pl.DataFrame | None = None,
    start: datetime = START,
    lines: bool = False,
) -> tuple[kw.WatchReport, Fake, RawStore, list[str]]:
    clock = kf.Clock(start)
    served = fake(clock)
    store = RawStore(tmp_path / "raw")
    echoed: list[str] = []
    slate = pf.slate() if games is None else games
    served.games = slate
    report = kw.watch(
        client=kf.make(store, served, clock),
        slate=lambda: slate,
        until=start + timedelta(minutes=minutes),
        echo=echoed.append,
        lines=lines,
    )
    return report, served, store, echoed
