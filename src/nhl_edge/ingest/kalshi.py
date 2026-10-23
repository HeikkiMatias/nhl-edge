"""Kalshi's order book (#282) for the markets of each day's alerted real bets, kept privately for
the owner's own resting orders (#281), whose gate needs the queue ahead of a resting bid at the
moment the order would be placed. Kalshi shows its book only live, so it is logged at the time.

**Terms.** Kalshi's Developer Agreement (v1.1) allows API use only to facilitate a member's own
trading, and keeping its data only for that, never shared with third parties without written
authorization (3.1); it bars benchmarking or competitive use (3.5) and asks for retries with
bounded exponential backoff (4.3). The owner is a Kalshi member, so:
- only both teams' markets of each game with an alerted bet (practice or real) are fetched,
  never all games, and never a game at or after its start;
- responses are kept only in the local raw store and the lake, mirrored to the private R2 bucket;
  nothing from Kalshi enters the public repository, its run logs, reports or fixtures: the poll,
  which a workflow runs, prints counts of requests only, never a price, quantity or market, and
  the replay, whose counts come from the books, is run by hand only;
- the table is never a model input: no feature or model code reads it.

Every game's markets are watched through the day apart from this, by the owner's decision of
2026-10-23 (#319, ingest/kalshi_watch.py), with this client's spacing and backoff.

**Source.** `GET /markets/{ticker}/orderbook` (public, no key) answers
`{"orderbook_fp": {"yes_dollars": [[price, contracts], ...], "no_dollars": [...]}}`, both as
strings, each list ascending by price, so the best Yes bid is the last yes entry and the best Yes
ask is 1 less the last no entry. Either list may be empty, null or missing. The response carries
no time, so observed_utc is the fetch time. A ticker is
`KXNHLGAME-{YY}{MON}{DD}{AWAY}{HOME}-{TEAM}` on the game's US Eastern date, with Kalshi's team
codes (lake/schemas.py KALSHI_CODES).

**Requests.** At least MIN_INTERVAL_S apart. A 404 is stored and counted as not found, never
retried or guessed further; 429, 5xx and transport errors are retried with exponential backoff,
at most TRIES tries, then counted as failed. Each response is stored untouched under
kalshi/orderbook/<game date>/<fetch stamp>_<ticker>, with its market in the sidecar, so
replay_books rebuilds kalshi_books without calling Kalshi.
"""

import json
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx
import polars as pl
from pandera.errors import SchemaError

from nhl_edge.ingest.nhl_api import USER_AGENT, parse_utc, retry_after_s
from nhl_edge.ingest.odds import ET, SLOT_PLANS
from nhl_edge.lake.raw import SUFFIX, RawStore
from nhl_edge.lake.schemas import KALSHI_CODES, KalshiBooks, dtypes
from nhl_edge.lake.tables import Lake
from nhl_edge.live.real_ledger import ALERTED

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
SOURCE = "kalshi"
BOOKS = "orderbook"
PREFIX = f"{SOURCE}/{BOOKS}"
TABLE = "kalshi_books"
SERIES = "KXNHLGAME"
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
MIN_INTERVAL_S = 0.25
TRIES = 5
# The waits between the tries: exponential and bounded (the agreement's 4.3).
BACKOFF_S = tuple(2.0**i for i in range(TRIES - 1))
MAX_WAIT_S = 30.0
TOO_MANY = 429
OK, NOT_FOUND = 200, 404
TIMEOUT_S = 10.0
# Each side's bids, by the key Kalshi lists them under.
SIDES = {"yes": "yes_dollars", "no": "no_dollars"}


class KalshiError(RuntimeError):
    """A request that failed after its tries or answered an unexpected status, or a response
    that is no order book. Its message names no market, since run logs are public."""


class Started(RuntimeError):
    """The market's game started before its request went out, so it is never fetched."""


@dataclass(frozen=True)
class Market:
    """One team's market of a game with an alerted bet: Yes pays if the team wins."""

    ticker: str
    season: int
    game_date: date
    game_id: int
    start_utc: datetime
    team: str  # NHL triCode


@dataclass(frozen=True)
class Fetched:
    market: Market
    status: int
    body: bytes
    raw_key: str
    fetched_utc: datetime


def clock() -> datetime:
    """The time to the microsecond: a fetch time cut to the second could date a book before it
    arrived, and an order placed in that second would then see it (hard rule 1)."""
    return datetime.now(UTC)


def kalshi_code(team: str) -> str:
    return KALSHI_CODES.get(team, team)


def ticker(game_date: date, away: str, home: str, team: str) -> str:
    """The market of team in away at home on game_date (US Eastern), such as
    KXNHLGAME-26OCT17NJWSH-WSH."""
    day = f"{game_date:%y}{MONTHS[game_date.month - 1]}{game_date:%d}"
    return f"{SERIES}-{day}{kalshi_code(away)}{kalshi_code(home)}-{kalshi_code(team)}"


def markets(alerts: pl.DataFrame, now: datetime) -> list[Market]:
    """Both teams' markets of each game with an alerted bet (real_alerts rows with status
    "alerted", practice or real) that starts after now, soonest first: the only markets asked
    for. A skipped bet's game is never fetched."""
    games = (
        alerts.filter(pl.col("status") == ALERTED, pl.col("start_utc") > now)
        .unique("game_id")
        .sort("start_utc", "game_id")
    )
    return [
        Market(
            ticker(row["game_date"], row["away"], row["home"], team),
            row["season"],
            row["game_date"],
            row["game_id"],
            row["start_utc"],
            team,
        )
        for row in games.iter_rows(named=True)
        for team in (row["away"], row["home"])
    ]


def slot_of(now: datetime, plan: str = "free-tier") -> str | None:
    """The odds slot a run at now follows: the latest slot of now's US Eastern day that started
    at or before it, or None before the first. A dispatched run has no cron line, so its raw
    copies are labelled from the clock."""
    et_time = now.astimezone(ET).time()
    begun = [slot.name for slot in SLOT_PLANS[plan] if slot.et_time <= et_time]
    return begun[-1] if begun else None


class Kalshi:
    """Fetches a market's order book and stores the response raw before returning it."""

    def __init__(
        self,
        store: RawStore,
        client: httpx.Client | None = None,
        *,
        now: Callable[[], datetime] = clock,
        min_interval_s: float = MIN_INTERVAL_S,
        backoff_s: tuple[float, ...] = BACKOFF_S,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.client = client or httpx.Client(
            base_url=BASE_URL, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S
        )
        self.now = now
        self.min_interval_s = min_interval_s
        self.backoff_s = backoff_s
        self.sleep = sleep
        self.monotonic = monotonic
        self._last_request = float("-inf")
        self.requests = 0

    def _throttle(self) -> None:
        wait = self._last_request + self.min_interval_s - self.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._last_request = self.monotonic()

    def request(
        self,
        path: str,
        params: Mapping[str, str | int] | None = None,
        *,
        deadline: datetime | None = None,
    ) -> tuple[httpx.Response, int]:
        """GET path, throttled: the response, a 200 or a 404, never retried, and the tries it
        took. 429, 5xx and transport errors are tried again after exponentially longer waits, at
        most TRIES tries in all. With a deadline, a game's start, the clock is checked just
        before each try, and nothing is asked at or after it (Started)."""
        error = ""
        for attempt, backoff in enumerate((*self.backoff_s, None), start=1):
            self._throttle()
            if deadline is not None and self.now() >= deadline:
                raise Started("the game started before the request")
            self.requests += 1
            retry_after: float | None = None
            try:
                response = self.client.get(path, params=params)
            except httpx.TransportError as exc:
                error = type(exc).__name__
            except httpx.HTTPError as exc:
                raise KalshiError(f"the request failed: {type(exc).__name__}") from None
            else:
                if response.status_code in (OK, NOT_FOUND):
                    return response, attempt
                if not retryable(response.status_code):
                    raise KalshiError(f"status {response.status_code}")
                error = f"status {response.status_code}"
                retry_after = retry_after_s(response.headers.get("Retry-After"), self.now())
            if backoff is None:
                break
            self.sleep(min(max(backoff, retry_after or 0.0), MAX_WAIT_S))
        raise KalshiError(f"failed after {len(self.backoff_s) + 1} tries: {error}")

    def orderbook(self, market: Market, slot: str | None) -> Fetched:
        """GET /markets/{ticker}/orderbook, stored raw with its fetch time; a 404 is stored too.
        A game that has started is never asked for (Started)."""
        path = f"/markets/{market.ticker}/{BOOKS}"
        response, attempt = self.request(path, deadline=market.start_utc)
        return self._store(market, response, attempt, slot)

    def _store(
        self, market: Market, response: httpx.Response, attempt: int, slot: str | None
    ) -> Fetched:
        fetched_utc = self.now()
        meta = {
            "url": str(response.request.url),
            "status": response.status_code,
            "fetched_utc": fetched_utc.isoformat(),
            "attempts": attempt,
            "slot": slot,
            "ticker": market.ticker,
            "season": market.season,
            "game_date": market.game_date.isoformat(),
            "game_id": market.game_id,
            "start_utc": market.start_utc.isoformat(),
            "team": market.team,
        }
        key = f"{BOOKS}/{market.game_date.isoformat()}/{fetched_utc:%Y%m%dT%H%M%SZ}_{market.ticker}"
        raw_key = self.store.put(SOURCE, key, response.content, meta)
        return Fetched(market, response.status_code, response.content, raw_key, fetched_utc)


def retryable(status: int) -> bool:
    """Whether a status is worth another try: a 429, or any server error (5xx)."""
    return status == TOO_MANY or 500 <= status <= 599


def market_of(meta: Mapping[str, Any]) -> Market:
    """The market a stored response was fetched for, from its sidecar."""
    return Market(
        meta["ticker"],
        int(meta["season"]),
        date.fromisoformat(meta["game_date"]),
        int(meta["game_id"]),
        parse_utc(meta["start_utc"]),
        meta["team"],
    )


def levels(body: bytes) -> dict[str, list[tuple[float, float]]]:
    """Each side's bids in an order book response, as (price in dollars, contracts) in the order
    listed. A side whose list is empty, null or missing has none; a response without orderbook_fp,
    or whose orderbook_fp is neither null nor an object, is no order book."""
    data = json.loads(body)
    if not isinstance(data, dict) or "orderbook_fp" not in data:
        raise KalshiError("no orderbook_fp in the response")
    book = {} if data["orderbook_fp"] is None else data["orderbook_fp"]
    if not isinstance(book, dict):
        raise KalshiError("orderbook_fp is no object")
    return {
        side: [(float(price), float(count)) for price, count in book.get(key) or []]
        for side, key in SIDES.items()
    }


def parse_orderbook(
    body: bytes, market: Market, observed_utc: datetime, raw_key: str
) -> pl.DataFrame:
    """One row per price level and side of a market's order book fetched at observed_utc. A level
    with no contracts is none. A response fetched at or after the start gives no rows."""
    columns = dtypes(KalshiBooks)
    if observed_utc >= market.start_utc:
        return pl.DataFrame(schema=columns)
    rows = [
        {
            "observed_utc": observed_utc,
            "season": market.season,
            "game_date": market.game_date,
            "game_id": market.game_id,
            "start_utc": market.start_utc,
            "team": market.team,
            "ticker": market.ticker,
            "side": side,
            "price": price,
            "quantity": quantity,
            "raw_key": raw_key,
        }
        for side, listed in levels(body).items()
        for price, quantity in listed
        if quantity > 0
    ]
    return KalshiBooks.validate(pl.DataFrame(rows, schema=columns))  # type: ignore[return-value]


def books_before(books: pl.DataFrame, at: datetime) -> pl.DataFrame:
    """The book rows an order placed at `at` may have seen: those observed strictly before it
    (hard rule 1)."""
    return books.filter(pl.col("observed_utc") < at)


@dataclass
class PollReport:
    markets: int = 0
    stored: int = 0  # order books stored (status 200)
    not_found: int = 0
    started: int = 0
    failed: int = 0


def run_books(
    *, kalshi: Kalshi, alerts: pl.DataFrame, slot: str | None, echo: Callable[[str], None]
) -> PollReport:
    """Fetch and store the order book of both teams' markets of each alerted bet's game that
    hasn't started. The raw copies are the record; replay_books builds the table from them.
    Only counts are echoed: run logs are public, and Kalshi's data stays private (3.1)."""
    report = PollReport()
    wanted = markets(alerts, kalshi.now())
    report.markets = len(wanted)
    if not wanted:
        echo("kalshi books: no alerted bet's game is still to start, nothing fetched")
        return report
    for market in wanted:
        try:
            fetched = kalshi.orderbook(market, slot)
        except Started:
            report.started += 1
            continue
        except KalshiError as exc:
            echo(f"::warning::kalshi books: a market failed ({exc})")
            report.failed += 1
            continue
        if fetched.status == NOT_FOUND:
            report.not_found += 1
            continue
        try:
            parse_orderbook(fetched.body, market, fetched.fetched_utc, fetched.raw_key)
        except (KalshiError, KeyError, TypeError, ValueError, SchemaError):
            echo("::warning::kalshi books: a stored response is no order book")
            report.failed += 1
            continue
        report.stored += 1
    if report.not_found:
        echo(
            f"::warning::kalshi books: {report.not_found} markets not found (404), kept raw: "
            "check their tickers"
        )
    echo(
        f"kalshi books: {report.markets} markets of {report.markets // 2} games, "
        f"{report.stored} books stored; {report.not_found} not found, {report.failed} failed, "
        f"{report.started} skipped as started"
    )
    return report


@dataclass
class ReplayReport:
    responses: int = 0
    rows: int = 0
    not_found: int = 0
    empty: int = 0
    after_start: int = 0
    unparsed: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    dates: list[date] = field(default_factory=list)


def stored_books(
    store: RawStore, dates: Collection[date] | None = None
) -> tuple[pl.DataFrame, ReplayReport]:
    """kalshi_books' rows for the given game dates (every stored date when None), parsed from the
    raw responses without writing the lake. Never calls Kalshi."""
    report = ReplayReport()
    frames = []
    root = store.base_dir / PREFIX
    day_dirs = sorted(root.iterdir()) if root.is_dir() else []
    for day_dir in day_dirs:
        try:
            day = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if dates is not None and day not in dates:
            continue
        report.dates.append(day)
        for path in sorted(day_dir.glob(f"*{SUFFIX}")):
            raw_key = path.relative_to(store.base_dir).as_posix().removesuffix(SUFFIX)
            if not (store.base_dir / f"{raw_key}.meta.json").exists():
                report.incomplete.append(raw_key)
                continue
            meta = store.meta(raw_key)
            report.responses += 1
            if meta.get("status") == NOT_FOUND:
                report.not_found += 1
                continue
            try:
                market = market_of(meta)
                observed_utc = parse_utc(meta["fetched_utc"])
                frame = parse_orderbook(store.get(raw_key), market, observed_utc, raw_key)
            except (KalshiError, KeyError, TypeError, ValueError, SchemaError):
                report.unparsed.append(raw_key)
                continue
            if observed_utc >= market.start_utc:
                report.after_start += 1
            elif frame.is_empty():
                report.empty += 1
            frames.append(frame)
    table = pl.concat(frames) if frames else pl.DataFrame(schema=dtypes(KalshiBooks))
    report.rows = table.height
    return table, report


def replay_books(
    store: RawStore, lake: Lake, dates: Collection[date] | None = None
) -> ReplayReport:
    """Rebuild kalshi_books for the given game dates (every stored date when None) from the raw
    responses. Each requested date's partition is replaced, and deleted when it has no rows, so a
    parser fix leaves nothing stale. Never calls Kalshi."""
    table, report = stored_books(store, dates)
    requested = sorted(dates) if dates is not None else report.dates
    lake.replace_dates(TABLE, table, requested)
    return report
