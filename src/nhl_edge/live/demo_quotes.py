"""Two-sided quoting on Kalshi's demo exchange (#375, ADR 0055): the paper simulator's rules
(live/kalshi_quotes.py) at one fair value and one half-spread, live, with mock funds. It tests the
mechanics (posting, moving, pulling and cancelling quotes in time, from where the jobs run), never
the economics: the demo's books and fills are other demo users', so adverse selection and profit
stay the simulator's, on the real logs.

**Each step** (STEP apart, from the morning view's publication to each game's start less
STOP_BEFORE), per game, in order:
1. **Fills:** each resting bid is read back; its new fills add to the team's contracts bought and to
   the day's filled cost at the maker fee. A bid no longer resting is dropped.
2. **Pulls:** a game's bids are cancelled when Daily Faceoff names another starter for either team
   than it named before (STARTER), or when a team's mid moved PULL_MOVE or more since its bid's
   posting (MOVE). The game then waits for the fair value to update after the pull.
3. **Quotes:** each team's Yes market gets a post-only bid of SIZE contracts at the fair value less
   HALF_SPREAD, rounded down to the cent as the simulator's rule does (#376 from its ROUNDED_FROM)
   and at least a cent below the demo market's best ask; a
   bid is replaced when its price or size would change. The size is what is left to NET_CAP of
   the team's net contracts, and no bid is posted past DAY_CAP of the day's filled and resting
   cost. A bid expires at its game's start less STOP_BEFORE on Kalshi's side too.

**The fair value (FAIR):** the model, the owner's choice: the morning view's blend from its
publication, then the decision's (kalshi_quotes.model_sources), each read only after its
publication (hard rule 1), from what R2 holds by then.

**Safety:** it runs only on the demo exchange (KALSHI_TRADING=demo). Its orders carry the CLIENT
prefix and their own record, apart from the practice orders. A refused call (#347: Kalshi refuses
trading from some US states) is recorded by its status; a refused cancel is tried again at the
next step, and REFUSED_ALERT in a row are alerted. Each run cancels its resting bids at its end.

**The record:** each run writes its events once (real/demo/quotes/<date>/<run>.json), and the next
run of the day reads them back for its holds and the starters named; the contracts bought come
back from Kalshi's own orders. Every response is kept raw by live/kalshi_trade.py. The log prints
counts only: prices, sizes and order ids stay in the record (#280).
"""

import io
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import polars as pl
from pandera.errors import SchemaError

from nhl_edge.ingest import dailyfaceoff as dfo
from nhl_edge.ingest.kalshi import ticker
from nhl_edge.ingest.nhl_api import parse_utc
from nhl_edge.lake.raw import RawStore
from nhl_edge.live import bundle as lb
from nhl_edge.live import exchange as ex
from nhl_edge.live import kalshi_quotes as kq
from nhl_edge.live import morning as lm
from nhl_edge.live.kalshi_trade import KalshiTradeError, OrderRequest, Placed
from nhl_edge.live.predict import ledger_key
from nhl_edge.live.real import Exchange

VERSION = "demo_quotes/1"
FAIR = kq.MODEL
HALF_SPREAD = Decimal("0.01")
SIZE, NET_CAP, DAY_CAP = kq.SIZE, kq.NET_CAP, kq.DAY_CAP
STOP_BEFORE, PULL_MOVE = kq.STOP_BEFORE, kq.PULL_MOVE
STARTER, MOVE = kq.STARTER, kq.MOVE
STEP = timedelta(minutes=1)
# Daily Faceoff is polled this often for the starters, as the bids' re-valuation polls it.
POLL_EVERY = timedelta(minutes=10)
PREFIX = "real/demo/quotes"
CLIENT = "q"
REFUSED_ALERT = 2
RESTING = "resting"


class Trading(Protocol):
    """What quoting asks of the exchange (live/kalshi_trade.KalshiTrade)."""

    def market(self, ticker: str) -> dict[str, Any]: ...

    def create_order(self, order: OrderRequest) -> Placed: ...

    def order(self, order_id: str) -> dict[str, Any]: ...

    def cancel(self, order_id: str, ticker: str) -> dict[str, Any] | None: ...

    def orders(self, ticker: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Sources:
    """What a step reads besides the exchange: each game's fair value (home side) and its
    publication, the latest published before a moment, and the starters Daily Faceoff names for a
    game's teams by then, each None without one."""

    fair: Callable[[kq.Game, datetime], tuple[float, datetime] | None]
    named: Callable[[kq.Game, datetime], dict[str, int | None] | None]


@dataclass
class Bid:
    """A resting bid: its market, order, limit, size, contracts filled, posting and the mid then."""

    game_id: int
    team: str
    ticker: str
    order_id: str
    client_order_id: str
    price: Decimal
    size: Decimal
    filled: Decimal
    posted_utc: datetime
    mid: Decimal | None

    @property
    def left(self) -> Decimal:
        return self.size - self.filled


@dataclass
class Day:
    """One game day's quoting: the resting bids, the contracts bought and the cost of the fills,
    each game's hold after a pull and the starters named at its last check, the bids whose cancel
    was refused, and what happened (events, counts)."""

    day: date
    run: str
    bids: dict[tuple[int, str], Bid] = field(default_factory=dict)
    bought: dict[tuple[int, str], Decimal] = field(default_factory=dict)
    filled_cost: Decimal = Decimal(0)
    held: dict[int, tuple[datetime, str]] = field(default_factory=dict)
    named: dict[int, dict[str, int | None]] = field(default_factory=dict)
    stuck: list[Bid] = field(default_factory=list)
    refused_in_a_row: int = 0
    serial: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)

    def event(self, at: datetime, kind: str, **values: Any) -> None:
        self.counts[kind] += 1
        self.events.append({"at": at.isoformat(), "kind": kind, **values})

    def resting_cost(self, rules: Exchange) -> Decimal:
        """The cost of every bid that can still fill: the resting ones, and those whose cancel
        was refused (Codex on #377)."""
        bids = [*self.bids.values(), *self.stuck]
        return sum((ex.cost(b.left, b.price, rules, maker=True) for b in bids), Decimal(0))

    def unresolved(self, key: tuple[int, str]) -> bool:
        """Whether a bid on the market waits for its cancel: no replacement goes up beside it."""
        return any((b.game_id, b.team) == key for b in self.stuck)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else Decimal(0)


def top(trading: Trading, market: str) -> tuple[Decimal | None, Decimal | None] | None:
    """A demo market's best Yes bid and ask, None when it can't be read or isn't open."""
    try:
        found = trading.market(market)
    except KalshiTradeError:
        return None
    if found.get("status") != "active":
        return None
    bid, ask = found.get("yes_bid_dollars"), found.get("yes_ask_dollars")
    return (
        Decimal(str(bid)) if bid not in (None, "") else None,
        Decimal(str(ask)) if ask not in (None, "") else None,
    )


def mid(found: tuple[Decimal | None, Decimal | None] | None) -> Decimal | None:
    if found is None or found[0] is None or found[1] is None:
        return None
    return (found[0] + found[1]) / 2


def client_id(day: Day, game: kq.Game, team: str) -> str:
    """A new bid's client order id: CLIENT, the day, the game, the team, the run and a serial."""
    day.serial += 1
    return f"{CLIENT}-{day.day:%Y%m%d}-{game.game_id}-{team}-{day.run}-{day.serial}"


def cancel(day: Day, trading: Trading, bid: Bid, at: datetime, why: str, rules: Exchange) -> None:
    """Cancel a bid, then read its fills once more, so a fill since the last read counts before
    the bid is forgotten (Codex on #377). A refused cancel is kept to try again at the next
    step, and its bid still counts as one that can fill."""
    try:
        trading.cancel(bid.order_id, bid.ticker)
    except KalshiTradeError as exc:
        day.stuck.append(bid)
        day.refused_in_a_row += 1
        day.event(at, "cancel_refused", ticker=bid.ticker, order_id=bid.order_id, status=exc.status)
        return
    day.refused_in_a_row = 0
    day.event(at, "cancelled", ticker=bid.ticker, order_id=bid.order_id, why=why)
    _read_fills(day, trading, bid, at, rules)


def _drop(
    day: Day, trading: Trading, key: tuple[int, str], at: datetime, why: str, rules: Exchange
) -> None:
    bid = day.bids.pop(key, None)
    if bid is not None:
        cancel(day, trading, bid, at, why, rules)


def _read_fills(day: Day, trading: Trading, bid: Bid, at: datetime, rules: Exchange) -> bool:
    """A bid's new fills into the day; whether it still rests."""
    try:
        order = trading.order(bid.order_id)
    except KalshiTradeError as exc:
        day.event(at, "read_refused", ticker=bid.ticker, order_id=bid.order_id, status=exc.status)
        return True
    filled = _decimal(order.get("fill_count_fp"))
    if filled > bid.filled:
        new = filled - bid.filled
        key = (bid.game_id, bid.team)
        day.bought[key] = day.bought.get(key, Decimal(0)) + new
        day.filled_cost += ex.cost(new, bid.price, rules, maker=True)
        bid.filled = filled
        day.event(at, "filled", ticker=bid.ticker, order_id=bid.order_id, contracts=str(new),
                  price=str(bid.price))  # fmt: skip
    return order.get("status") == RESTING


def _size(day: Day, game: kq.Game, team: str) -> Decimal:
    """A team's next bid: SIZE, or what is left to NET_CAP of its net contracts."""
    bought = day.bought.get((game.game_id, team), Decimal(0))
    net = bought - day.bought.get((game.game_id, game.other(team)), Decimal(0))
    return min(SIZE, NET_CAP - net)


def changed(before: Mapping[str, int | None] | None, now: Mapping[str, int | None] | None) -> bool:
    """Whether Daily Faceoff names another starter for either team than before."""
    if before is None or now is None:
        return False
    return any(
        before.get(team) is not None and now.get(team) is not None and now[team] != before[team]
        for team in before
    )


def step(
    day: Day,
    games: Sequence[kq.Game],
    at: datetime,
    sources: Sources,
    trading: Trading,
    rules: Exchange,
) -> None:
    """One step of every game at at: fills, pulls and quotes, soonest game first. A bid whose
    cancel was refused has its fills read first, and is cancelled again if it still rests."""
    for bid in list(day.stuck):
        day.stuck.remove(bid)
        if _read_fills(day, trading, bid, at, rules):
            cancel(day, trading, bid, at, "retry", rules)
    for game in sorted(games, key=lambda g: (g.start_utc, g.game_id)):
        keys = [(game.game_id, team) for team in (game.home, game.away)]
        for key in keys:
            bid = day.bids.get(key)
            if bid is not None and not _read_fills(day, trading, bid, at, rules):
                del day.bids[key]
        if at >= game.start_utc - STOP_BEFORE:
            for key in keys:
                _drop(day, trading, key, at, "stop", rules)
            continue
        named = sources.named(game, at)
        before = day.named.get(game.game_id)
        if changed(before, named):
            for key in keys:
                _drop(day, trading, key, at, STARTER, rules)
            day.held[game.game_id] = (at, STARTER)
            day.event(at, "pulled", game_id=game.game_id, why=STARTER)
        if named is not None:
            # A poll naming no starter for a team keeps the last one named, as the simulator's
            # change_times does, so a later change still pulls (Codex on #377).
            kept = dict(before or {})
            kept.update({team: goalie for team, goalie in named.items() if goalie is not None})
            day.named[game.game_id] = {team: kept.get(team) for team in (game.home, game.away)}
        tops = {
            key: top(trading, ticker(game.game_date, game.away, game.home, key[1])) for key in keys
        }
        for key in keys:
            bid = day.bids.get(key)
            now_mid = mid(tops[key])
            before = bid.mid if bid is not None else None
            if before is not None and now_mid is not None and abs(now_mid - before) >= PULL_MOVE:
                for each in keys:
                    _drop(day, trading, each, at, MOVE, rules)
                day.held[game.game_id] = (at, MOVE)
                day.event(at, "pulled", game_id=game.game_id, why=MOVE)
                break
        fair = sources.fair(game, at)
        hold = day.held.get(game.game_id)
        if fair is not None and hold is not None and fair[1] > hold[0]:
            del day.held[game.game_id]
            hold = None
        for key in keys:
            team = key[1]
            if fair is None or hold is not None or tops[key] is None:
                _drop(day, trading, key, at, "no quote", rules)
                continue
            found = tops[key]
            assert found is not None
            price = kq._quote_price(
                game.side(team, fair[0]), HALF_SPREAD, found[1], game.game_date >= kq.ROUNDED_FROM
            )
            size = _size(day, game, team)
            bid = day.bids.get(key)
            if price is None or size <= 0:
                _drop(day, trading, key, at, "no quote", rules)
                continue
            if bid is not None and bid.price == price and bid.left == size:
                continue
            _drop(day, trading, key, at, "replaced", rules)
            if day.unresolved(key):
                day.counts["waiting_on_cancel"] += 1
                continue
            # The cancel's last read may have added fills: the size is what is left now (Codex
            # on #377).
            size = _size(day, game, team)
            if size <= 0:
                continue
            planned = ex.cost(size, price, rules, maker=True)
            if day.filled_cost + day.resting_cost(rules) + planned > DAY_CAP:
                day.counts["capped"] += 1
                continue
            market = ticker(game.game_date, game.away, game.home, team)
            request = OrderRequest(
                ticker=market,
                client_order_id=client_id(day, game, team),
                count=size,
                price=price,
                expiration_time=game.start_utc - STOP_BEFORE,
                post_only=True,
            )
            try:
                placed = trading.create_order(request)
            except KalshiTradeError as exc:
                day.event(at, "post_refused", ticker=market, status=exc.status, code=exc.code)
                continue
            day.bids[key] = Bid(
                game.game_id, team, market, placed.order_id, request.client_order_id, price, size,
                Decimal(0), at, mid(found),
            )  # fmt: skip
            day.event(at, "posted", ticker=market, order_id=placed.order_id, price=str(price),
                      contracts=str(size), fair=fair[0], fair_utc=fair[1].isoformat())  # fmt: skip


def cancel_all(day: Day, trading: Trading, at: datetime, rules: Exchange) -> None:
    """Every resting bid cancelled, as a run ends, its last fills read."""
    for key in list(day.bids):
        _drop(day, trading, key, at, "end of run", rules)
    for bid in list(day.stuck):
        day.stuck.remove(bid)
        if _read_fills(day, trading, bid, at, rules):
            cancel(day, trading, bid, at, "end of run", rules)


def recover(
    day: Day, trading: Trading, games: Sequence[kq.Game], at: datetime, rules: Exchange
) -> None:
    """The day's contracts bought and filled cost from Kalshi's own record of its CLIENT orders on
    each game's markets, and any bid an earlier run left resting cancelled."""
    prefix = f"{CLIENT}-{day.day:%Y%m%d}-"
    for game in games:
        for team in (game.home, game.away):
            market = ticker(game.game_date, game.away, game.home, team)
            try:
                found = trading.orders(market)
            except KalshiTradeError as exc:
                day.event(at, "read_refused", ticker=market, status=exc.status)
                continue
            for order in found:
                if not str(order.get("client_order_id", "")).startswith(prefix):
                    continue
                filled = _decimal(order.get("fill_count_fp"))
                price = _decimal(order.get("yes_price_dollars"))
                if filled > 0:
                    key = (game.game_id, team)
                    day.bought[key] = day.bought.get(key, Decimal(0)) + filled
                    day.filled_cost += ex.cost(filled, price, rules, maker=True)
                if order.get("status") == RESTING:
                    # Its fills so far are counted: the cancel's last read adds only later ones.
                    # Its size is those and what still rests, so a refused cancel counts what can
                    # still fill (Codex on #377).
                    size = filled + _decimal(order.get("remaining_count_fp"))
                    left = Bid(game.game_id, team, market, order["order_id"],
                               order["client_order_id"], price, size, filled, at,
                               None)  # fmt: skip
                    cancel(day, trading, left, at, "earlier run", rules)


# The sources, from R2


def fair_reader(
    store: lb.Store, day: date
) -> Callable[[kq.Game, datetime], tuple[float, datetime] | None]:
    """The model's fair value (kalshi_quotes.model_sources): the morning view's blend, then the
    decision's, each from R2 once written and only after its publication. A file is read once it
    exists, so a step before it is written sees none."""
    held: dict[str, pl.DataFrame] = {}

    def frames() -> tuple[pl.DataFrame, pl.DataFrame]:
        for name, key in (("morning", lm.key(day)), ("ledger", ledger_key(day))):
            if name not in held and key in store.keys(key.rsplit("/", 1)[0]):
                held[name] = pl.read_parquet(io.BytesIO(store.get(key)))
        empty = pl.DataFrame(
            schema={"game_id": pl.Int64, "status": pl.String, "p_blend": pl.Float64,
                    "published_utc": pl.Datetime("us", "UTC")}
        )  # fmt: skip
        return held.get("ledger", empty), held.get("morning", empty)

    def fair(game: kq.Game, at: datetime) -> tuple[float, datetime] | None:
        ledger, morning = frames()
        found = kq.latest(kq.model_sources(ledger, morning).get(game.game_id, []), at)
        return (found[1], found[0]) if found is not None else None

    return fair


def games_of(store: lb.Store, day: date) -> list[kq.Game]:
    """The day's games from its morning view's rows, none before they are written."""
    key = lm.key(day)
    if key not in store.keys(lm.PREFIX):
        return []
    return kq.slate_of(lm.stored(store, day))


def starters_reader(raw: RawStore) -> Callable[[kq.Game, datetime], dict[str, int | None] | None]:
    """The starters Daily Faceoff names for a game's teams in its newest page stored at or before a
    moment and fetched by then, as revalue.starters reads them: each team's listing nearest the
    game's start. None without a page."""

    def named(game: kq.Game, at: datetime) -> dict[str, int | None] | None:
        key = raw.latest_before(f"{dfo.PREFIX}/{game.game_date.isoformat()}", at)
        if key is None:
            return None
        try:
            observed = parse_utc(raw.meta(key)["fetched_utc"])
            rows = dfo.parse_starting_goalies(raw.get(key), observed, key)
        except (dfo.DailyFaceoffError, KeyError, TypeError, ValueError, SchemaError):
            return None
        if observed > at:
            return None
        found: dict[str, int | None] = {}
        for team in (game.home, game.away):
            listed = rows.filter(pl.col("team") == team)
            if listed.height:
                gap = (pl.col("start_utc") - game.start_utc).abs()
                found[team] = listed.sort(gap).row(0, named=True)["dfo_goalie_id"]
        return {team: found.get(team) for team in (game.home, game.away)}

    return named


# The stop


def stop_key(day: date) -> str:
    """The day's stop marker: once written, no run quotes that day again."""
    return f"{PREFIX}/{day.isoformat()}/day.stop"


def stopped(store: lb.Store, day: date) -> bool:
    return stop_key(day) in store.keys(f"{PREFIX}/{day.isoformat()}")


def stop(store: lb.Store, day: date, at: datetime) -> None:
    """Write the day's stop marker (the kill switch, kalshi-cancel.yml), once."""
    if not stopped(store, day):
        lb.put_once(store, stop_key(day), json.dumps({"stopped_utc": at.isoformat()}).encode())


def cancel_resting(
    trading: Trading, games: Sequence[kq.Game], day: date, at: datetime
) -> tuple[int, int]:
    """Every resting CLIENT bid of the day on each game's markets cancelled, from Kalshi's own
    list of orders: the bids cancelled and the calls refused."""
    prefix, done, refused = f"{CLIENT}-{day:%Y%m%d}-", 0, 0
    for game in games:
        for team in (game.home, game.away):
            market = ticker(game.game_date, game.away, game.home, team)
            try:
                listed = trading.orders(market)
            except KalshiTradeError:
                refused += 1
                continue
            for order in listed:
                ours = str(order.get("client_order_id", "")).startswith(prefix)
                if not ours or order.get("status") != RESTING:
                    continue
                try:
                    trading.cancel(order["order_id"], market)
                except KalshiTradeError:
                    refused += 1
                    continue
                done += 1
    return done, refused


# The record


def run_key(day: date, run: str) -> str:
    return f"{PREFIX}/{day.isoformat()}/{run}.json"


def keep(store: lb.Store, day: Day, ended: datetime) -> str:
    """The run's events and counts, written once."""
    key = run_key(day.day, day.run)
    body = {
        "version": VERSION,
        "fair": FAIR,
        "half_spread": str(HALF_SPREAD),
        "day": day.day.isoformat(),
        "run": day.run,
        "ended_utc": ended.isoformat(),
        "counts": dict(day.counts),
        "held": {str(k): [v[0].isoformat(), v[1]] for k, v in day.held.items()},
        "named": {str(k): v for k, v in day.named.items()},
        "events": day.events,
    }
    lb.put_once(store, key, json.dumps(body, sort_keys=True).encode())
    return key


def runs(store: lb.Store, day: date) -> list[dict[str, Any]]:
    """The day's kept runs, in order."""
    keys = sorted(k for k in store.keys(f"{PREFIX}/{day.isoformat()}") if k.endswith(".json"))
    return [json.loads(store.get(k)) for k in keys]


def resumed(store: lb.Store, day: date, run: str) -> Day:
    """A new run's day, with the holds and the starters named from the day's earlier runs."""
    state = Day(day, run)
    for earlier in runs(store, day):
        for game_id, (at, why) in earlier.get("held", {}).items():
            state.held[int(game_id)] = (datetime.fromisoformat(at), why)
        for game_id, named in earlier.get("named", {}).items():
            state.named[int(game_id)] = named
    return state


def summary(kept: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """A day's runs summed: each kind's count, and the bids still resting at a run's end, which
    the end of each run cancels."""
    counts: Counter[str] = Counter()
    for each in kept:
        counts.update(each.get("counts", {}))
    return {"runs": len(kept), **dict(sorted(counts.items()))}


def count_line(day: Day) -> str:
    """The log's line: counts only, never a price, a size or an order id (#280)."""
    shown = ", ".join(f"{n} {kind}" for kind, n in sorted(day.counts.items()))
    return f"demo quotes {day.day}: {shown or 'nothing'}; {len(day.bids)} resting"


def now() -> datetime:
    return datetime.now(UTC)
