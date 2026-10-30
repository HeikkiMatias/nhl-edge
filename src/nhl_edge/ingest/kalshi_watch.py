"""Every game's Kalshi markets, watched from the evening before to the start (#319), for choosing
the owner's own orders. The owner decided on 2026-10-23 to log every game's books and trades, not
only the alerted bets' markets (#282), as the real-time source for re-valuing resting orders
(#321) and for the shadow that selects on Kalshi's own prices (#320); and on 2026-10-30 to watch
earlier, and Kalshi's goal totals and player props beside the winner (#357, #358, #359).

**When.** The timer starts a run at each of WINDOWS_ET, every 4 hours, and each stops a minute
before the next, so the watch runs all day. A run watches its US Eastern date's games, and from
EVENING the next date's too. A market within NEAR of its start is polled at the full cadence
(TOP_EVERY, BOOK_EVERY), one further out at FAR_TOP_EVERY and FAR_BOOK_EVERY.

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

**Lines and props** (kalshi_lines, with lines=True). The top of every other market of a game,
read from Kalshi's listings, never a book or a trade:
- LISTING every LISTING_EVERY: every open market of GAME_SERIES (the winner, the total, the
  spread and the team totals), one `GET /markets?series_ticker=…&status=open` each, so a line is
  seen from its market's open;
- LINES every LINES_EVERY: the LINE_SERIES markets of the games within NEAR, from the latest
  listing, `tickers=` in requests of CHUNK;
- PROPS every PROPS_EVERY while a game within NEAR is still to start: every open market of
  PROP_SERIES, which Kalshi lists on the game day only.

Each is stored as `kalshi/watch/<kind>/<poll's US Eastern date>/<stamp>`, with the slate's games
in the sidecar. A listing holds the markets of games already under way too: parse_lines keeps a
row only for a game of the slate before its start, or one on a later date than the slate's.
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
from nhl_edge.lake.schemas import (
    KALSHI_LINES_KEY,
    KalshiDepth,
    KalshiLines,
    KalshiTops,
    KalshiTrades,
    dtypes,
)
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
# A market further than NEAR from its start, as the next day's are in the evening.
NEAR = timedelta(hours=12)
FAR_TOP_EVERY = timedelta(minutes=15)
FAR_BOOK_EVERY = timedelta(minutes=30)
OVERLAP = timedelta(minutes=1)
REFRESH = timedelta(minutes=30)
PAGE = 1000
MAX_PAGES = 20
# The windows the timer dispatches a run at, in US Eastern time (infra/timer), every 4 hours: a
# run ends a minute before the next one starts, and never after LONGEST, inside GitHub's 6-hour
# job limit.
WINDOWS_ET = (time(3, 5), time(7, 5), time(11, 5), time(15, 5), time(19, 5), time(23, 5))
LONGEST = timedelta(hours=5, minutes=45)
# From this window on, a run watches the next US Eastern date's games too.
EVENING = time(19, 5)

LISTING, LINES, PROPS = "listing", "lines", "props"
LINE_KINDS = (LISTING, LINES, PROPS)
LINES_TABLE = "kalshi_lines"
# The game-level series a listing reads, from each market's open; LINE_SERIES are polled at the
# lines' cadence near the start. PROP_SERIES are listed on the game day only.
LINE_SERIES = ("KXNHLTOTAL", "KXNHLSPREAD", "KXNHLTEAMTOTAL")
GAME_SERIES = (kalshi.SERIES, *LINE_SERIES)
PROP_SERIES = ("KXNHLGOAL", "KXNHLPTS", "KXNHLAST", "KXNHLFIRSTGOAL", "KXNHLSAVES")
LISTING_EVERY = timedelta(minutes=30)
LINES_EVERY = timedelta(minutes=5)
PROPS_EVERY = timedelta(minutes=15)
# Tickers per tops request: a request's URL stays short.
CHUNK = 100


# GitHub's fallback cron lines (kalshi-watch.yml), one per UTC offset of US Eastern time: a line
# fires all year, so a run checks that its line is the one of today's offset.
FALLBACK_CRONS = {
    "5 3,7,11,15,19,23 * * *": timedelta(hours=-4),  # EDT
    "5 0,4,8,12,16,20 * * *": timedelta(hours=-5),  # EST
}


def cron_due(cron: str, now: datetime) -> bool:
    """Whether a fallback cron line is the one of now's US Eastern offset; an unknown line never
    is."""
    return FALLBACK_CRONS.get(cron) == now.astimezone(ET).utcoffset()


def window_end(now: datetime) -> datetime:
    """When a run started at now stops: a minute before the next window, the next US Eastern
    day's first after the day's last, and never more than LONGEST after now."""
    local = now.astimezone(ET)
    starts = [
        datetime.combine(local.date() + timedelta(days=d), at, tzinfo=ET)
        for d in (0, 1)
        for at in WINDOWS_ET
    ]
    following = min(start for start in starts if start > local)
    return min((following - timedelta(minutes=1)).astimezone(UTC), now + LONGEST)


def watched_days(now: datetime) -> list[date]:
    """The US Eastern dates whose games a run watches at now: its own, and from EVENING the
    next."""
    local = now.astimezone(ET)
    later = [local.date() + timedelta(days=1)] if local.time() >= EVENING else []
    return [local.date(), *later]


def watch_from(market: Market) -> datetime:
    """When a market's watch begins: EVENING of the US Eastern day before its game. Its first
    trades poll of each run reaches back to it, so a poll that failed at the end of an earlier
    run leaves no gap (Codex on #325)."""
    evening = datetime.combine(market.game_date - timedelta(days=1), EVENING, tzinfo=ET)
    return evening.astimezone(UTC)


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
    """One response of a poll, with its own fetch time: ticker is None for the tops listing. A
    200 whose body lacks the kind's documented list is kept raw but marked invalid: the replay
    skips it, and the poll counts it failed."""

    ticker: str | None
    status: int
    body: bytes
    fetched_utc: datetime
    attempts: int
    valid: bool = True


# Each kind's required field (Kalshi's documented schemas): a listing's markets, a book's
# orderbook_fp, a trades page's trades (Codex on #325).
REQUIRED = {TOPS: "markets", BOOKS: "orderbook_fp", TRADES: "trades"}


def shaped(kind: str, body: bytes) -> bool:
    """Whether a 200's body is the kind's documented object: an object holding its required
    field, a list for the listing and the trades."""
    try:
        data = json.loads(body)
    except ValueError:
        return False
    if not isinstance(data, dict) or REQUIRED[kind] not in data:
        return False
    return kind == BOOKS or isinstance(data[REQUIRED[kind]], list)


def _answer(
    kind: str, ticker: str | None, response: Any, attempts: int, client: kalshi.Kalshi
) -> Response:
    ok = response.status_code != kalshi.OK or shaped(kind, response.content)
    return Response(ticker, response.status_code, response.content, client.now(), attempts, ok)


@dataclass
class WatchReport:
    """A run's counts: polls stored by kind, requests, and the markets not found, failed or
    skipped as started. Kalshi's data never shows in them."""

    polls: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    line_polls: dict[str, int] = field(default_factory=lambda: dict.fromkeys(LINE_KINDS, 0))
    not_found: int = 0
    failed: int = 0
    started: int = 0
    refresh_failed: int = 0
    dates: set[date] = field(default_factory=set)
    # The US Eastern dates of the line polls stored.
    line_dates: set[date] = field(default_factory=set)

    @property
    def stored(self) -> int:
        return sum(self.polls.values()) + sum(self.line_polls.values())


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
                    "body": r.body.decode(errors="replace"),
                    **({} if r.valid else {"invalid": True}),
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
    report.failed += sum(not r.valid for r in responses)


def _deadline(market: Market, until: datetime) -> datetime:
    """A request's deadline: its game's start, or the window's end if sooner, so no request,
    try or retry starts after the window (Codex on #325)."""
    return min(market.start_utc, until)


def poll_tops(
    client: kalshi.Kalshi, live: Sequence[Market], until: datetime, report: WatchReport
) -> None:
    """The top of book of every market still to start, in one request; a market that starts
    while it is asked for is dropped and the request made again without it. Nothing is asked at
    or after until."""
    wanted = list(live)
    polled = client.now()
    while wanted:
        tickers = ",".join(m.ticker for m in wanted)
        try:
            response, attempts = client.request(
                "/markets",
                {"tickers": tickers, "limit": PAGE},
                deadline=min(_deadline(m, until) for m in wanted),
            )
        except Started:
            if client.now() >= until:
                return
            report.started += 1
            wanted = [m for m in wanted if m.start_utc > client.now()]
            continue
        except KalshiError:
            report.failed += 1
            return
        found = _answer(TOPS, None, response, attempts, client)
        _count(report, [found])
        store_poll(client.store, TOPS, wanted, [found], polled)
        report.polls[TOPS] += 1
        report.dates.add(wanted[0].game_date)
        return


def poll_books(
    client: kalshi.Kalshi, live: Sequence[Market], until: datetime, report: WatchReport
) -> None:
    """Each market's full book, stored together as one poll; none is asked at or after until."""
    polled, found, asked = client.now(), [], []
    for market in live:
        try:
            response, attempts = client.request(
                f"/markets/{market.ticker}/{kalshi.BOOKS}", deadline=_deadline(market, until)
            )
        except Started:
            if client.now() >= until:
                break
            report.started += 1
            continue
        except KalshiError:
            report.failed += 1
            continue
        found.append(_answer(BOOKS, market.ticker, response, attempts, client))
        asked.append(market)
    _count(report, found)
    if store_poll(client.store, BOOKS, asked, found, polled) is not None:
        report.polls[BOOKS] += 1
        report.dates.add(asked[0].game_date)


def poll_trades(
    client: kalshi.Kalshi,
    live: Sequence[Market],
    since: Mapping[str, datetime],
    until: datetime,
    report: WatchReport,
) -> set[str]:
    """Each market's trades since its own watermark less OVERLAP, every page of them, stored
    together as one poll; nothing is asked at or after until. Returns the markets whose every
    page came back in the documented shape: only their watermarks may advance, so a failed,
    malformed or cut-short market is asked again from the same point (Codex on #325)."""
    polled, found, asked, done = client.now(), [], [], set()
    for market in live:
        if client.now() >= until:
            break
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
                    "/markets/trades", params, deadline=_deadline(market, until)
                )
            except Started:
                if client.now() < until:
                    report.started += 1
                break
            except KalshiError:
                report.failed += 1
                break
            answer = _answer(TRADES, market.ticker, response, attempts, client)
            found.append(answer)
            if market not in asked:
                asked.append(market)
            if not answer.valid:
                break
            if response.status_code != kalshi.OK:
                done.add(market.ticker)
                break
            cursor = json.loads(response.content).get("cursor") or None
            if not cursor:
                done.add(market.ticker)
                break
    _count(report, found)
    if store_poll(client.store, TRADES, asked, found, polled) is not None:
        report.polls[TRADES] += 1
        report.dates.add(asked[0].game_date)
    return done


def _game_meta(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "game_id": row["game_id"],
        "season": row["season"],
        "game_date": row["game_date"].isoformat(),
        "start_utc": row["start_utc"].isoformat(),
        "away": row["away"],
        "home": row["home"],
    }


def store_lines(
    store: RawStore,
    kind: str,
    games: pl.DataFrame,
    days: Sequence[date],
    responses: Sequence[Response],
    polled_utc: datetime,
) -> str | None:
    """One line poll's responses as one raw object, each body untouched with its fetch time, and
    the slate's games and the dates they cover in the sidecar, which parse_lines reads; None when
    the poll got no response."""
    if not responses:
        return None
    body = json.dumps(
        {
            "responses": [
                {
                    "asked": r.ticker,
                    "status": r.status,
                    "fetched_utc": r.fetched_utc.isoformat(),
                    "attempts": r.attempts,
                    "body": r.body.decode(errors="replace"),
                    **({} if r.valid else {"invalid": True}),
                }
                for r in responses
            ]
        }
    ).encode()
    day = polled_utc.astimezone(ET).date()
    meta = {
        "kind": kind,
        "observed_date": day.isoformat(),
        "polled_utc": polled_utc.isoformat(),
        "days": [d.isoformat() for d in days],
        "games": [_game_meta(row) for row in games.iter_rows(named=True)],
    }
    key = f"{WATCH}/{kind}/{day.isoformat()}/{polled_utc:%Y%m%dT%H%M%SZ}"
    return store.put(kalshi.SOURCE, key, body, meta)


def _listed(
    client: kalshi.Kalshi, series: Sequence[str], until: datetime, report: WatchReport
) -> list[Response]:
    """Every open market of each series, every page of it by its cursor: the responses, each
    with its series as what was asked. Nothing is asked at or after until."""
    found: list[Response] = []
    for name in series:
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, str | int] = {"series_ticker": name, "status": "open", "limit": PAGE}
            if cursor:
                params["cursor"] = cursor
            try:
                response, attempts = client.request("/markets", params, deadline=until)
            except Started:
                return found
            except KalshiError:
                report.failed += 1
                break
            answer = _answer(TOPS, name, response, attempts, client)
            found.append(answer)
            if not answer.valid or response.status_code != kalshi.OK:
                break
            cursor = json.loads(response.content).get("cursor") or None
            if not cursor:
                break
    return found


def line_tickers(responses: Sequence[Response]) -> dict[str, list[str]]:
    """The LINE_SERIES markets of a listing's responses, by event ticker."""
    events: dict[str, list[str]] = {}
    for response in responses:
        if response.ticker not in LINE_SERIES or not response.valid or response.status != 200:
            continue
        for item in json.loads(response.body).get("markets", []):
            if isinstance(item, dict) and item.get("event_ticker") and item.get("ticker"):
                events.setdefault(str(item["event_ticker"]), []).append(str(item["ticker"]))
    return events


def event_tickers(game: Mapping[str, Any], series: Sequence[str]) -> list[str]:
    """A game's event ticker in each series."""
    key = kalshi.event_key(game["game_date"], game["away"], game["home"])
    return [f"{name}-{key}" for name in series]


def poll_lines(
    client: kalshi.Kalshi,
    games: pl.DataFrame,
    days: Sequence[date],
    events: Mapping[str, Sequence[str]],
    until: datetime,
    report: WatchReport,
) -> None:
    """The top of book of every LINE_SERIES market of the games (those within NEAR, not yet
    started), from the latest listing's events, CHUNK tickers a request, stored as one poll. A
    request's deadline is the first start among its games."""
    polled, found = client.now(), []
    for game in games.sort("start_utc").iter_rows(named=True):
        tickers = [t for e in event_tickers(game, LINE_SERIES) for t in events.get(e, [])]
        for at in range(0, len(tickers), CHUNK):
            chunk = tickers[at : at + CHUNK]
            try:
                response, attempts = client.request(
                    "/markets",
                    {"tickers": ",".join(chunk), "limit": PAGE},
                    deadline=min(game["start_utc"], until),
                )
            except Started:
                if client.now() < until:
                    report.started += 1
                break
            except KalshiError:
                report.failed += 1
                continue
            found.append(_answer(TOPS, None, response, attempts, client))
    _count(report, found)
    if store_lines(client.store, LINES, games, days, found, polled) is not None:
        report.line_polls[LINES] += 1
        report.line_dates.add(polled.astimezone(ET).date())


def poll_listing(
    client: kalshi.Kalshi,
    kind: str,
    series: Sequence[str],
    games: pl.DataFrame,
    days: Sequence[date],
    until: datetime,
    report: WatchReport,
) -> list[Response]:
    """Every open market of each series (kind LISTING or PROPS), stored as one poll; the
    responses, for the lines' tickers."""
    polled = client.now()
    found = _listed(client, series, until, report)
    _count(report, found)
    if store_lines(client.store, kind, games, days, found, polled) is not None:
        report.line_polls[kind] += 1
        report.line_dates.add(polled.astimezone(ET).date())
    return found


def _next(due: datetime, every: timedelta, now: datetime) -> datetime:
    """The first time after now on due's grid of every: a poll that overran skips the times it
    missed rather than catching them up in a burst."""
    while due <= now:
        due += every
    return due


def _by_date(markets: Sequence[Market]) -> list[list[Market]]:
    """The markets split by game date, each poll's own, soonest first."""
    days: dict[date, list[Market]] = {}
    for market in sorted(markets, key=lambda m: (m.game_date, m.start_utc, m.ticker)):
        days.setdefault(market.game_date, []).append(market)
    return list(days.values())


def watch(
    *,
    client: kalshi.Kalshi,
    slate: Callable[[], pl.DataFrame],
    until: datetime,
    echo: Callable[[str], None],
    top_every: timedelta = TOP_EVERY,
    book_every: timedelta = BOOK_EVERY,
    far_top_every: timedelta = FAR_TOP_EVERY,
    far_book_every: timedelta = FAR_BOOK_EVERY,
    refresh: timedelta = REFRESH,
    lines: bool = False,
) -> WatchReport:
    """Poll the slate's markets until `until` or every game's start, whichever comes first. A
    market within NEAR of its start has its top every top_every and its book and trades every
    book_every; one further out every far_top_every and far_book_every. Each game date's markets
    are a poll of their own. With lines, also the listings, the lines and the props
    (kalshi_lines). The slate is fetched again every refresh; a failed refetch keeps the last
    one. Only counts are echoed."""
    report = WatchReport()
    start = client.now()
    games, refreshed = slate(), start
    # Each market's trades watermark: the start of its last trades poll whose every page came
    # back. A run's first poll of a market reaches back to the start of its watch (watch_from),
    # so a trades poll that failed at the end of the run before leaves no gap (Codex on #325);
    # the trades seen twice are kept once, by trade_id.
    since: dict[str, datetime] = {}
    cadence = {"near": (top_every, book_every), "far": (far_top_every, far_book_every)}
    next_top = dict.fromkeys(cadence, start)
    next_book = dict.fromkeys(cadence, start)
    next_listing = next_lines = next_props = start
    events: dict[str, list[str]] = {}
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
        tiers = {
            "near": [m for m in live if m.start_utc - now <= NEAR],
            "far": [m for m in live if m.start_utc - now > NEAR],
        }
        for tier, group in tiers.items():
            if not group:
                continue
            tops_every, books_every = cadence[tier]
            if now >= next_top[tier]:
                for same_day in _by_date(group):
                    poll_tops(client, same_day, until, report)
                next_top[tier] = _next(next_top[tier], tops_every, client.now())
            if now >= next_book[tier]:
                began = client.now()
                for same_day in _by_date(group):
                    poll_books(client, same_day, until, report)
                    marks = {
                        m.ticker: since.get(m.ticker, min(start - books_every, watch_from(m)))
                        for m in same_day
                    }
                    for ticker in poll_trades(client, same_day, marks, until, report):
                        since[ticker] = began
                next_book[tier] = _next(next_book[tier], books_every, client.now())
        wake = [until, *(m.start_utc - NEAR for m in tiers["far"])]
        wake += [due[t] for due in (next_top, next_book) for t, g in tiers.items() if g]
        if lines:
            days = watched_days(now)
            upcoming = games.filter(pl.col("start_utc") > now)
            near = upcoming.filter(pl.col("start_utc") - now <= NEAR)
            if now >= next_listing:
                listed = poll_listing(client, LISTING, GAME_SERIES, upcoming, days, until, report)
                events = line_tickers(listed) or events
                next_listing = _next(next_listing, LISTING_EVERY, client.now())
            if near.height and now >= next_lines:
                poll_lines(client, near, days, events, until, report)
                next_lines = _next(next_lines, LINES_EVERY, client.now())
            if near.height and now >= next_props:
                poll_listing(client, PROPS, PROP_SERIES, upcoming, days, until, report)
                next_props = _next(next_props, PROPS_EVERY, client.now())
            wake += [next_listing, *((next_lines, next_props) if near.height else ())]
        wait = min(wake) - client.now()
        if wait > timedelta(0):
            client.sleep(wait.total_seconds())
    tail = ""
    if lines:
        tail = (
            f"; {report.line_polls[LISTING]} listing, {report.line_polls[LINES]} line and "
            f"{report.line_polls[PROPS]} prop polls"
        )
    echo(
        f"kalshi watch: {report.polls[TOPS]} top, {report.polls[BOOKS]} book and "
        f"{report.polls[TRADES]} trade polls stored in {client.requests} requests; "
        f"{report.not_found} not found, {report.failed} failed, {report.started} skipped as "
        f"started{tail}"
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


# Every NHL team's triCode, for reading an event ticker's teams back.
NHL_TEAMS = (
    "ANA", "BOS", "BUF", "CAR", "CBJ", "CGY", "CHI", "COL", "DAL", "DET", "EDM", "FLA", "LAK",
    "MIN", "MTL", "NJD", "NSH", "NYI", "NYR", "OTT", "PHI", "PIT", "SEA", "SJS", "STL", "TBL",
    "TOR", "UTA", "VAN", "VGK", "WPG", "WSH",
)  # fmt: skip
FROM_KALSHI = {kalshi.kalshi_code(team): team for team in NHL_TEAMS}


@dataclass(frozen=True)
class Event:
    """What an event ticker names: its series, its game's US Eastern date and its teams (None
    when they can't be read)."""

    series: str
    game_date: date
    away: str | None
    home: str | None


def parse_event(event_ticker: str) -> Event | None:
    """An event ticker such as KXNHLTOTAL-26NOV01OTTANA read back: the date is its first seven
    characters after the series, and the teams the one split of the rest into two known codes.
    None when the date can't be read."""
    series, _, rest = event_ticker.partition("-")
    day, teams = rest[:7], rest[7:]
    try:
        month = kalshi.MONTHS.index(day[2:5]) + 1
        game_date = date(2000 + int(day[:2]), month, int(day[5:7]))
    except ValueError:
        return None
    splits = [
        (FROM_KALSHI[teams[:i]], FROM_KALSHI[teams[i:]])
        for i in (2, 3)
        if teams[:i] in FROM_KALSHI and teams[i:] in FROM_KALSHI
    ]
    away, home = splits[0] if len(splits) == 1 else (None, None)
    return Event(series, game_date, away, home)


def _named(suffix: str, event: Event, series: str) -> tuple[str | None, str | None]:
    """The team and, for a prop, the player tag a market's ticker suffix names: the event's team
    whose code it starts with, and what follows it, without the prop's threshold."""
    for team in (event.away, event.home):
        code = kalshi.kalshi_code(team) if team else None
        if code and suffix.startswith(code):
            rest = suffix[len(code) :].split("-")[0]
            return team, (rest or None) if series in PROP_SERIES else None
    return None, None


def parse_lines(
    body: bytes,
    kind: str,
    games: Sequence[Mapping[str, Any]],
    days: Collection[date],
    observed_utc: datetime,
    raw_key: str,
) -> pl.DataFrame:
    """One row per listed market of an NHL game before its start (KalshiLines): a game of the
    poll's slate (games, the sidecar's) only before its start, one on a date after the slate's
    dates (days) always, and one on the slate's dates the slate lacks never, since its start
    isn't known."""
    data = json.loads(body)
    listed = data.get("markets") if isinstance(data, dict) else None
    if not isinstance(listed, list):
        raise KalshiError("no markets list in the response")
    by_teams = {(g["game_date"], g["away"], g["home"]): g for g in games}
    last = max(days)
    rows = []
    for item in listed:
        if not isinstance(item, dict):
            continue
        ticker, event_ticker = str(item.get("ticker")), str(item.get("event_ticker"))
        event = parse_event(event_ticker)
        if event is None or not ticker.startswith(f"{event_ticker}-"):
            continue
        game = by_teams.get((event.game_date.isoformat(), event.away, event.home))
        start = parse_utc(game["start_utc"]) if game else None
        if start is not None and observed_utc >= start:
            continue
        if start is None and event.game_date <= last:
            continue
        team, player = _named(ticker[len(event_ticker) + 1 :], event, event.series)
        rows.append(
            {
                "observed_utc": observed_utc,
                "observed_date": observed_utc.astimezone(ET).date(),
                "kind": kind,
                "series": event.series,
                "event_ticker": event_ticker,
                "ticker": ticker,
                "game_date": event.game_date,
                "away": event.away,
                "home": event.home,
                "game_id": game["game_id"] if game else None,
                "start_utc": start,
                "team": team,
                "player_tag": player,
                "subject": item.get("yes_sub_title"),
                "strike_type": item.get("strike_type"),
                "floor_strike": _number(item.get("floor_strike")),
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
    frame = pl.DataFrame(rows, schema=dtypes(KalshiLines))
    return KalshiLines.validate(frame)  # type: ignore[return-value]


@dataclass
class ReplayReport:
    objects: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    rows: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    not_found: int = 0
    invalid: int = 0
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
                        if response.get("invalid"):
                            report.invalid += 1
                            continue
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


def stored_lines(
    store: RawStore, dates: Collection[date] | None = None
) -> tuple[pl.DataFrame, ReplayReport]:
    """kalshi_lines' rows for the given poll dates (US Eastern; every stored date when None),
    parsed from the raw line polls without writing the lake. Never calls Kalshi."""
    report = ReplayReport(objects=dict.fromkeys(LINE_KINDS, 0), rows=dict.fromkeys(LINE_KINDS, 0))
    frames: list[pl.DataFrame] = []
    seen: set[date] = set()
    for kind in LINE_KINDS:
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
                    days = [date.fromisoformat(d) for d in meta["days"]]
                    for response in json.loads(store.get(raw_key))["responses"]:
                        if response.get("invalid"):
                            report.invalid += 1
                            continue
                        if response["status"] != kalshi.OK:
                            report.not_found += response["status"] == kalshi.NOT_FOUND
                            continue
                        parsed = parse_lines(
                            response["body"].encode(),
                            kind,
                            meta["games"],
                            days,
                            parse_utc(response["fetched_utc"]),
                            raw_key,
                        )
                        report.rows[kind] += parsed.height
                        frames.append(parsed)
                except (KalshiError, KeyError, TypeError, ValueError, SchemaError):
                    report.unparsed.append(raw_key)
    report.dates = sorted(seen)
    columns = dtypes(KalshiLines)
    table = pl.concat(frames) if frames else pl.DataFrame(schema=columns)
    # A market a poll's pages listed twice is kept once.
    table = table.unique(list(KALSHI_LINES_KEY), keep="first").sort(*KALSHI_LINES_KEY)
    return KalshiLines.validate(table), report  # type: ignore[return-value]


def replay_lines(
    store: RawStore, lake: Lake, dates: Collection[date] | None = None
) -> ReplayReport:
    """Rebuild kalshi_lines for the given poll dates (every stored date when None) from the raw
    line polls, as replay does the three tables: a poll it can't read stops it before any
    partition is replaced (ReplayError). Never calls Kalshi."""
    table, report = stored_lines(store, dates)
    if report.unparsed or report.incomplete:
        raise ReplayError(
            f"{len(report.unparsed)} unparsed and {len(report.incomplete)} incomplete stored "
            "line polls: no partition replaced"
        )
    requested = sorted(dates) if dates is not None else report.dates
    lake.replace_dates(LINES_TABLE, table, requested)
    return report
