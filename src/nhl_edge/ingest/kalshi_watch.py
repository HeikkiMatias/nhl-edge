"""Every game's Kalshi markets, watched from the morning to the start (#319), for choosing the
owner's own orders. The owner decided on 2026-10-23 to log every game's books and trades, not only
the alerted bets' markets (#282), as the real-time source for re-valuing resting orders (#321)
and for the shadow that selects on Kalshi's own prices (#320).

**Terms.** As #282 (ingest/kalshi.py), within the Developer Agreement's own-trading use (3.1):
- both teams' markets of each game of the day's slate, never a game at or after its start;
- the responses stay in the local raw store and the lake, mirrored to the private R2 bucket;
  nothing from Kalshi enters the public repository, its run logs, reports or fixtures, and a run
  prints counts of requests only, never a price, a quantity, a trade or a market;
- the tables are never a model input or a CLV reference.

**Polls.** Kalshi's public REST API, through ingest/kalshi.Kalshi's spacing and bounded backoff:
- TOPS every TOP_EVERY: `GET /markets?tickers=…` for the markets still to start, in one request:
  each one's best Yes bid and ask with the contracts at each, last price, volume and open
  interest;
- BOOKS every BOOK_EVERY: `GET /markets/{ticker}/orderbook` for each;
- TRADES with the books: `GET /markets/trades?ticker=…&min_ts=…` for each, paged by its cursor,
  from the previous trades poll's start less OVERLAP, so no trade between two polls is missed. A
  trade seen twice is kept once, at its first fetch.

Each poll's responses are stored as one raw object, `kalshi/watch/<kind>/<game date>/<stamp>`,
each response with its own fetch time, so a parse never dates a response before it arrived (hard
rule 1). The slate is fetched again every REFRESH, so a game moved or postponed is followed.
replay rebuilds kalshi_tops, kalshi_depth and kalshi_trades from the raw objects, never calling
Kalshi.
"""

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import polars as pl
from pandera.errors import SchemaError

from nhl_edge.ingest import kalshi
from nhl_edge.ingest.kalshi import KalshiError, Market, Started
from nhl_edge.ingest.nhl_api import parse_utc
from nhl_edge.ingest.odds import ET
from nhl_edge.lake.raw import SUFFIX, RawStore
from nhl_edge.lake.schemas import KalshiDepth, KalshiTops, KalshiTrades, dtypes
from nhl_edge.lake.tables import Lake

WATCH = "watch"
TOPS, BOOKS, TRADES = "tops", "books", "trades"
KINDS = (TOPS, BOOKS, TRADES)
TABLES = {TOPS: "kalshi_tops", BOOKS: "kalshi_depth", TRADES: "kalshi_trades"}
SCHEMAS = {TOPS: KalshiTops, BOOKS: KalshiDepth, TRADES: KalshiTrades}
ORDER = {
    TOPS: ("game_date", "ticker", "observed_utc"),
    BOOKS: ("game_date", "ticker", "observed_utc", "side", "price"),
    TRADES: ("game_date", "ticker", "created_utc", "trade_id"),
}
TOP_EVERY = timedelta(minutes=1)
BOOK_EVERY = timedelta(minutes=5)
OVERLAP = timedelta(minutes=1)
REFRESH = timedelta(minutes=30)
PAGE = 1000
MAX_PAGES = 20
# The windows the timer dispatches a run at, in US Eastern time (infra/timer): a run ends a minute
# before the next one starts, and the last after LONGEST, inside GitHub's 6-hour job limit.
WINDOWS_ET = (time(7, 5), time(11, 5), time(15, 5), time(19, 5))
LONGEST = timedelta(hours=5, minutes=45)


# GitHub's fallback cron lines (kalshi-watch.yml), one per UTC offset of US Eastern time: a line
# fires all year, so a run checks that its line is the one of today's offset.
FALLBACK_CRONS = {
    "5 11,15,19,23 * * *": timedelta(hours=-4),  # EDT
    "5 12,16,20,0 * * *": timedelta(hours=-5),  # EST
}


def watch_hours(now: datetime) -> bool:
    """Whether a run started at now may watch: from the first window's start to midnight, US
    Eastern. A run started after midnight is a late fallback whose day is over, and the next one
    starts at the first window (Codex on #325)."""
    return now.astimezone(ET).time() >= WINDOWS_ET[0]


def cron_due(cron: str, now: datetime) -> bool:
    """Whether a fallback cron line is the one of now's US Eastern offset; an unknown line never
    is."""
    return FALLBACK_CRONS.get(cron) == now.astimezone(ET).utcoffset()


def window_end(now: datetime) -> datetime:
    """When a run started at now stops: a minute before the next window of now's US Eastern
    day, and never more than LONGEST after now."""
    local = now.astimezone(ET)
    later = [start for start in WINDOWS_ET if start > local.time()]
    cap = now + LONGEST
    if not later:
        return cap
    end = datetime.combine(local.date(), later[0], tzinfo=ET) - timedelta(minutes=1)
    return min(end.astimezone(UTC), cap)


def markets_of(games: pl.DataFrame) -> list[Market]:
    """Both teams' markets of each game of the slate (game_id, season, game_date, start_utc,
    home, away), soonest first."""
    rows = games.sort("start_utc", "game_id").iter_rows(named=True)
    return [
        Market(
            kalshi.ticker(row["game_date"], row["away"], row["home"], team),
            row["season"],
            row["game_date"],
            row["game_id"],
            row["start_utc"],
            team,
        )
        for row in rows
        for team in (row["away"], row["home"])
    ]


def _market_meta(market: Market) -> dict[str, Any]:
    return {
        "ticker": market.ticker,
        "season": market.season,
        "game_date": market.game_date.isoformat(),
        "game_id": market.game_id,
        "start_utc": market.start_utc.isoformat(),
        "team": market.team,
    }


@dataclass(frozen=True)
class Response:
    """One response of a poll, with its own fetch time: ticker is None for the tops listing."""

    ticker: str | None
    status: int
    body: bytes
    fetched_utc: datetime
    attempts: int


@dataclass
class WatchReport:
    """A run's counts: polls stored by kind, requests, and the markets not found, failed or
    skipped as started. Kalshi's data never shows in them."""

    polls: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    not_found: int = 0
    failed: int = 0
    started: int = 0
    refresh_failed: int = 0
    dates: set[date] = field(default_factory=set)

    @property
    def stored(self) -> int:
        return sum(self.polls.values())


def store_poll(
    store: RawStore,
    kind: str,
    markets: Sequence[Market],
    responses: Sequence[Response],
    polled_utc: datetime,
) -> str | None:
    """One poll's responses as one raw object, each body kept untouched as text with its fetch
    time, and the markets asked for in the sidecar; None when the poll got no response."""
    if not responses:
        return None
    day = markets[0].game_date
    body = json.dumps(
        {
            "responses": [
                {
                    "ticker": r.ticker,
                    "status": r.status,
                    "fetched_utc": r.fetched_utc.isoformat(),
                    "attempts": r.attempts,
                    "body": r.body.decode(),
                }
                for r in responses
            ]
        }
    ).encode()
    meta = {
        "kind": kind,
        "game_date": day.isoformat(),
        "polled_utc": polled_utc.isoformat(),
        "markets": [_market_meta(m) for m in markets],
    }
    key = f"{WATCH}/{kind}/{day.isoformat()}/{polled_utc:%Y%m%dT%H%M%SZ}"
    return store.put(kalshi.SOURCE, key, body, meta)


def _count(report: WatchReport, responses: Sequence[Response]) -> None:
    report.not_found += sum(r.status == kalshi.NOT_FOUND for r in responses)


def poll_tops(client: kalshi.Kalshi, live: Sequence[Market], report: WatchReport) -> None:
    """The top of book of every market still to start, in one request; a market that starts
    while it is asked for is dropped and the request made again without it."""
    wanted = list(live)
    polled = client.now()
    while wanted:
        tickers = ",".join(m.ticker for m in wanted)
        try:
            response, attempts = client.request(
                "/markets",
                {"tickers": tickers, "limit": PAGE},
                deadline=min(m.start_utc for m in wanted),
            )
        except Started:
            report.started += 1
            wanted = [m for m in wanted if m.start_utc > client.now()]
            continue
        except KalshiError:
            report.failed += 1
            return
        found = Response(None, response.status_code, response.content, client.now(), attempts)
        _count(report, [found])
        store_poll(client.store, TOPS, wanted, [found], polled)
        report.polls[TOPS] += 1
        report.dates.add(wanted[0].game_date)
        return


def poll_books(client: kalshi.Kalshi, live: Sequence[Market], report: WatchReport) -> None:
    """Each market's full book, stored together as one poll."""
    polled, found, asked = client.now(), [], []
    for market in live:
        try:
            response, attempts = client.request(
                f"/markets/{market.ticker}/{kalshi.BOOKS}", deadline=market.start_utc
            )
        except Started:
            report.started += 1
            continue
        except KalshiError:
            report.failed += 1
            continue
        found.append(
            Response(market.ticker, response.status_code, response.content, client.now(), attempts)
        )
        asked.append(market)
    _count(report, found)
    if store_poll(client.store, BOOKS, asked, found, polled) is not None:
        report.polls[BOOKS] += 1
        report.dates.add(asked[0].game_date)


def poll_trades(
    client: kalshi.Kalshi,
    live: Sequence[Market],
    since: Mapping[str, datetime],
    report: WatchReport,
) -> set[str]:
    """Each market's trades since its own watermark less OVERLAP, every page of them, stored
    together as one poll. Returns the markets whose every page came back: only their watermarks
    may advance, so a failed or cut-short market is asked again from the same point (Codex on
    #325)."""
    polled, found, asked, done = client.now(), [], [], set()
    for market in live:
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, str | int] = {
                "ticker": market.ticker,
                "min_ts": int((since[market.ticker] - OVERLAP).timestamp()),
                "limit": PAGE,
            }
            if cursor:
                params["cursor"] = cursor
            try:
                response, attempts = client.request(
                    "/markets/trades", params, deadline=market.start_utc
                )
            except Started:
                report.started += 1
                break
            except KalshiError:
                report.failed += 1
                break
            found.append(
                Response(
                    market.ticker, response.status_code, response.content, client.now(), attempts
                )
            )
            if market not in asked:
                asked.append(market)
            try:
                cursor = json.loads(response.content).get("cursor") or None
            except (ValueError, AttributeError):
                cursor = None
            if response.status_code != kalshi.OK or not cursor:
                done.add(market.ticker)
                break
    _count(report, found)
    if store_poll(client.store, TRADES, asked, found, polled) is not None:
        report.polls[TRADES] += 1
        report.dates.add(asked[0].game_date)
    return done


def _next(due: datetime, every: timedelta, now: datetime) -> datetime:
    """The first time after now on due's grid of every: a poll that overran skips the times it
    missed rather than catching them up in a burst."""
    while due <= now:
        due += every
    return due


def watch(
    *,
    client: kalshi.Kalshi,
    slate: Callable[[], pl.DataFrame],
    until: datetime,
    echo: Callable[[str], None],
    top_every: timedelta = TOP_EVERY,
    book_every: timedelta = BOOK_EVERY,
    refresh: timedelta = REFRESH,
) -> WatchReport:
    """Poll the slate's markets until `until` or every game's start, whichever comes first: the
    tops every top_every, the books and trades every book_every. The slate is fetched again every
    refresh; a failed refetch keeps the last one. Only counts are echoed."""
    report = WatchReport()
    start = client.now()
    games, refreshed = slate(), start
    # Each market's trades watermark: the start of its last trades poll whose every page came
    # back; a market's first poll reaches back one book interval.
    since: dict[str, datetime] = {}
    next_top = next_book = start
    while True:
        now = client.now()
        if now - refreshed >= refresh:
            try:
                games = slate()
            except Exception as exc:
                echo(f"::warning::kalshi watch: the slate refetch failed ({type(exc).__name__})")
                report.refresh_failed += 1
            refreshed = now
        live = [m for m in markets_of(games) if m.start_utc > now]
        if not live or now >= until:
            break
        if now >= next_top:
            poll_tops(client, live, report)
            next_top = _next(next_top, top_every, client.now())
        if now >= next_book:
            began = client.now()
            poll_books(client, live, report)
            marks = {m.ticker: since.get(m.ticker, start - book_every) for m in live}
            for ticker in poll_trades(client, live, marks, report):
                since[ticker] = began
            next_book = _next(next_book, book_every, client.now())
        wait = min(next_top, next_book, until) - client.now()
        if wait > timedelta(0):
            client.sleep(wait.total_seconds())
    echo(
        f"kalshi watch: {report.polls[TOPS]} top, {report.polls[BOOKS]} book and "
        f"{report.polls[TRADES]} trade polls stored in {client.requests} requests; "
        f"{report.not_found} not found, {report.failed} failed, {report.started} skipped as "
        f"started"
    )
    return report


def _number(value: object) -> float | None:
    return None if value is None or value == "" else float(str(value))


def parse_tops(
    body: bytes, markets: Mapping[str, Market], observed_utc: datetime, raw_key: str
) -> pl.DataFrame:
    """One row per asked market the listing gives, fetched at observed_utc before its start:
    its status, best Yes bid and ask with their sizes, last price, volume and open interest."""
    data = json.loads(body)
    listed = data.get("markets") if isinstance(data, dict) else None
    if not isinstance(listed, list):
        raise KalshiError("no markets list in the response")
    rows = []
    for item in listed:
        market = markets.get(str(item.get("ticker"))) if isinstance(item, dict) else None
        if market is None or observed_utc >= market.start_utc:
            continue
        rows.append(
            {
                "observed_utc": observed_utc,
                "season": market.season,
                "game_date": market.game_date,
                "game_id": market.game_id,
                "start_utc": market.start_utc,
                "team": market.team,
                "ticker": market.ticker,
                "status": item.get("status"),
                "yes_bid": _number(item.get("yes_bid_dollars")),
                "yes_bid_size": _number(item.get("yes_bid_size_fp")),
                "yes_ask": _number(item.get("yes_ask_dollars")),
                "yes_ask_size": _number(item.get("yes_ask_size_fp")),
                "last_price": _number(item.get("last_price_dollars")),
                "volume": _number(item.get("volume_fp")),
                "open_interest": _number(item.get("open_interest_fp")),
                "raw_key": raw_key,
            }
        )
    frame = pl.DataFrame(rows, schema=dtypes(KalshiTops))
    return KalshiTops.validate(frame)  # type: ignore[return-value]


def parse_trades(body: bytes, market: Market, observed_utc: datetime, raw_key: str) -> pl.DataFrame:
    """One row per trade of the market made before its start, fetched at observed_utc before the
    start: a trade is known from its fetch, never from its own time (hard rule 1)."""
    data = json.loads(body)
    trades = data.get("trades") if isinstance(data, dict) else None
    if trades is None:
        trades = []
    if not isinstance(trades, list):
        raise KalshiError("no trades list in the response")
    columns = dtypes(KalshiTrades)
    if observed_utc >= market.start_utc:
        return pl.DataFrame(schema=columns)
    rows = []
    for trade in trades:
        created = parse_utc(trade["created_time"])
        if created >= market.start_utc:
            continue
        rows.append(
            {
                "trade_id": str(trade["trade_id"]),
                "observed_utc": observed_utc,
                "created_utc": created,
                "season": market.season,
                "game_date": market.game_date,
                "game_id": market.game_id,
                "start_utc": market.start_utc,
                "team": market.team,
                "ticker": market.ticker,
                "yes_price": float(trade["yes_price_dollars"]),
                "count": float(trade["count_fp"]),
                "taker_side": trade.get("taker_outcome_side") or trade.get("taker_side"),
                "is_block_trade": trade.get("is_block_trade"),
                "raw_key": raw_key,
            }
        )
    frame = pl.DataFrame(rows, schema=columns).unique("trade_id", keep="first")
    return KalshiTrades.validate(frame)  # type: ignore[return-value]


@dataclass
class ReplayReport:
    objects: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    rows: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    not_found: int = 0
    unparsed: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    dates: list[date] = field(default_factory=list)


def _parse(
    kind: str,
    body: bytes,
    markets: Mapping[str, Market],
    ticker: str | None,
    observed: datetime,
    raw_key: str,
) -> pl.DataFrame:
    if kind == TOPS:
        return parse_tops(body, markets, observed, raw_key)
    market = markets[str(ticker)]
    if kind == BOOKS:
        books = kalshi.parse_orderbook(body, market, observed, raw_key)
        return KalshiDepth.validate(books)  # type: ignore[return-value]
    return parse_trades(body, market, observed, raw_key)


def stored_watch(
    store: RawStore, dates: Collection[date] | None = None
) -> tuple[dict[str, pl.DataFrame], ReplayReport]:
    """The three tables' rows for the given game dates (every stored date when None), parsed from
    the raw polls without writing the lake. Never calls Kalshi. A trade fetched by two polls is
    kept at its first fetch."""
    report = ReplayReport()
    frames: dict[str, list[pl.DataFrame]] = {kind: [] for kind in KINDS}
    seen: set[date] = set()
    for kind in KINDS:
        root = store.base_dir / kalshi.SOURCE / WATCH / kind
        for day_dir in sorted(root.iterdir()) if root.is_dir() else []:
            try:
                day = date.fromisoformat(day_dir.name)
            except ValueError:
                continue
            if dates is not None and day not in dates:
                continue
            seen.add(day)
            for path in sorted(day_dir.glob(f"*{SUFFIX}")):
                raw_key = path.relative_to(store.base_dir).as_posix().removesuffix(SUFFIX)
                if not (store.base_dir / f"{raw_key}.meta.json").exists():
                    report.incomplete.append(raw_key)
                    continue
                report.objects[kind] += 1
                try:
                    meta = store.meta(raw_key)
                    markets = {m["ticker"]: kalshi.market_of(m) for m in meta["markets"]}
                    polled = json.loads(store.get(raw_key))["responses"]
                    for response in polled:
                        if response["status"] != kalshi.OK:
                            report.not_found += response["status"] == kalshi.NOT_FOUND
                            continue
                        frames[kind].append(
                            _parse(
                                kind,
                                response["body"].encode(),
                                markets,
                                response["ticker"],
                                parse_utc(response["fetched_utc"]),
                                raw_key,
                            )
                        )
                except (KalshiError, KeyError, TypeError, ValueError, SchemaError):
                    report.unparsed.append(raw_key)
    report.dates = sorted(seen)
    tables = {}
    for kind in KINDS:
        columns = dtypes(SCHEMAS[kind])
        table = pl.concat(frames[kind]) if frames[kind] else pl.DataFrame(schema=columns)
        if kind == TRADES:
            table = table.sort("observed_utc").unique("trade_id", keep="first")
        table = table.sort(*ORDER[kind])
        tables[kind] = SCHEMAS[kind].validate(table)
        report.rows[kind] = table.height
    return tables, report


class ReplayError(RuntimeError):
    """A replay that met stored polls it couldn't read: the lake keeps its last partitions. Its
    message gives counts only."""


def replay(store: RawStore, lake: Lake, dates: Collection[date] | None = None) -> ReplayReport:
    """Rebuild kalshi_tops, kalshi_depth and kalshi_trades for the given game dates (every stored
    date when None) from the raw polls; each requested date's partition is replaced, and deleted
    when it has no rows. Never calls Kalshi. A poll that is unparsed or incomplete stops it before
    any partition is replaced (ReplayError), so a changed response shape never drops rows from
    the lake unnoticed (Codex on #325)."""
    tables, report = stored_watch(store, dates)
    if report.unparsed or report.incomplete:
        raise ReplayError(
            f"{len(report.unparsed)} unparsed and {len(report.incomplete)} incomplete stored "
            "polls: no partition replaced"
        )
    requested = sorted(dates) if dates is not None else report.dates
    for kind in KINDS:
        lake.replace_dates(TABLES[kind], tables[kind], requested)
    return report
