"""Synthetic Kalshi order books and a fake Kalshi for #282's tests, with no network.

Every number here and in fixtures/kalshi/ is made up, in the documented shape: the public
repository holds no Kalshi response (Kalshi's Developer Agreement, 3.1).
"""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from nhl_edge.ingest import kalshi
from nhl_edge.lake.raw import RawStore

# SYNTHETIC: three Yes and three No bids with made-up prices and sizes.
BOOK = (Path(__file__).parent / "fixtures" / "kalshi" / "orderbook_synthetic.json").read_bytes()
DAY = date(2026, 10, 7)
START = datetime(2026, 10, 7, 23, 30, tzinfo=UTC)
FETCHED = datetime(2026, 10, 7, 16, 55, tzinfo=UTC)
MARKET = kalshi.Market(
    kalshi.ticker(DAY, "PIT", "WSH", "WSH"), 20262027, DAY, 2026020053, START, "WSH"
)

Handler = Callable[[httpx.Request], httpx.Response]
# The client itself, kept apart from kalshi.Kalshi, which a CLI test replaces with make().
KALSHI = kalshi.Kalshi


def book(yes: object = None, no: object = None, *, drop: tuple[str, ...] = ()) -> bytes:
    """A synthetic response with the given lists (None stays null); drop leaves keys out."""
    lists = {"yes_dollars": yes, "no_dollars": no}
    return json.dumps({"orderbook_fp": {k: v for k, v in lists.items() if k not in drop}}).encode()


class Clock:
    """A fake clock: monotonic seconds and UTC time both move on only when slept, and every
    sleep is recorded."""

    def __init__(self, start: datetime = FETCHED) -> None:
        self.start = start
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds

    def now(self) -> datetime:
        return self.start + timedelta(seconds=self.t)

    def advance(self, delta: timedelta) -> None:
        self.t += delta.total_seconds()


def serve(body: bytes = BOOK, missing: frozenset[str] = frozenset()) -> tuple[list[str], Handler]:
    """Every market's book, and 404 for the tickers in missing; the paths asked for are kept."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        ticker = request.url.path.split("/")[-2]
        if ticker in missing:
            return httpx.Response(404, json={"code": "not_found", "message": "not found"})
        return httpx.Response(200, content=body)

    return paths, handler


def sequence(*responses: httpx.Response | Exception) -> tuple[list[httpx.Request], Handler]:
    requests: list[httpx.Request] = []
    pending = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = pending.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    return requests, handler


def fail(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no request expected, got {request.url}")


def make(
    store: RawStore, handler: Handler, clock: Clock | None = None, min_interval_s: float = 0.0
) -> kalshi.Kalshi:
    clock = clock or Clock()
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=kalshi.BASE_URL)
    return KALSHI(
        store,
        client,
        now=clock.now,
        min_interval_s=min_interval_s,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
