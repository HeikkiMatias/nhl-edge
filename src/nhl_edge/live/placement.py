"""Real bets placed automatically on Kalshi (ADR 0040 and 0045, #281, #321): each alerted bet's
buy order in two parts, their fills, and the bet's entry in the real record.

**The switch.** KALSHI_TRADING, a repository variable, is off, demo or live:
- **off:** nothing is placed, and a run cancels our resting orders;
- **demo:** every alerted bet, practice or real, goes to Kalshi's demo exchange (mock funds),
  recorded apart under real/demo/, so nothing from it enters the practice or the real record;
- **live:** the real days' alerted bets go to the live exchange, once the venue is Kalshi's
  (real.VENUE); a practice day places nothing live.

**The order** of an alerted bet is its alert row's buy order (the exchange column, written with the
day's alert) for a live order; a demo order is computed the same way at the demo exchange's rules
(real.DEMO_EXCHANGE), from the row's probability, hurdle and stake table. It goes to the team's
market (ingest/kalshi.ticker) in two parts (ADR 0045), each with a client order id derived from
its environment, mode, date, game, policy and part, so a retry or a rerun is the same order, which
Kalshi refuses twice (409):
- **the taker part:** an immediate-or-cancel buy at L_t, the limit at the taker fee, sized at L_t;
  what doesn't fill at once Kalshi cancels;
- **the maker part:** then, once the taker part's fills are read, a post-only bid at L_m, the
  highest cent that clears the hurdle at the maker fee, resting until the expiry. Its contracts
  keep both parts within the maker part's planned cost and within the stake the table allows at
  their effective price together (exchange.rest). A post-only bid can't rest at or above the best
  ask, and Kalshi refuses or cancels one that would: it is then sent a cent lower, down to L_t,
  since no Kalshi price is read to place it (ADR 0040 item 2). Each cent tried has its own client
  order id.

**Before each order** (place), all of these hold, or nothing is sent and the record says why:
- the game's start, as the schedule lists it now, is still the alert's (leakage audit of #281: an
  order resting into a game moved earlier would fill during it);
- the order expires at least LEAD from now, its game's start less the exchange's expire_before;
- its most cost, the larger of its two parts' (the taker part's fills leave the maker part only the
  rest of its own planned cost), is within the frozen policy's max_bet of the bankroll, and the
  day's orders together, an earlier run's included, within max_day;
- **the code's own limits** (#294), whatever the alert row in R2 says: the environment's exchange
  is one the code names (DEMO_EXCHANGE, or the live venue's in real.VENUES), at the row's rate;
  each part's cost, recomputed from its contracts and limit under those rules (the maker part's at
  the maker fee, all a post-only order can pay), is no more than its row states; and the order's
  most cost, and the day's together, are within max_bet and max_day of CEILING_BANKROLL at the
  code's rate. Anyone who could rewrite R2 could otherwise size the orders;
- the balance covers them;
- the market is active.

Each planned order is written to the record (orders/<date>/<game_id>.json) before any check, a
skipped one's reason after its check (...-skipped.json), and Kalshi's order id of each part once it
is sent (...-taker-placed.json, ...-maker-placed.json), or why no maker part went
(...-maker-skipped.json). A rerun sends nothing for a part already placed, and sends the maker
part of a bet whose run stopped after its taker part. ADR 0040's orders, one resting order each
(...-placed.json), are tracked to their end as before.

**Its deadline** is its expiry, or earlier if the schedule moves its game earlier; the earliest
start any run has seen holds (...-moved-<start>.json), so one unread schedule loses no move. Each
run (reconcile) cancels a resting part whose deadline has passed. Once it has, and in a run that
read the schedule, each part is read with its fills and the order is written once (...-final.json).
The bet is its parts' fills before the start, the earliest seen, each tagged taker or maker by
Kalshi's is_taker; a fill at or after it, which the expiry rules out, is kept apart, flagged, and
never the bet's (hard rule 2). On the live exchange, the bet's entry in the real record follows
(real/live/entries/), as the dashboard's form would write it:
- placed, at the matched price contracts / cost, fees included, with the stake cost / rate in
  euros, the order ids of the parts that filled and the first fill's time;
- not placed, not matched, when nothing filled before the start;
- not placed, missed it, when no order went.

**Logs** print counts only (run logs are public). The record holds the rest, and the owner's
Telegram gets the details.
"""

import contextlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from nhl_edge.betting.selection import POLICY
from nhl_edge.ingest.kalshi import ticker
from nhl_edge.lake.schemas import RealOrders, dtypes
from nhl_edge.live import bundle as lb
from nhl_edge.live import kalshi_trade as kt
from nhl_edge.live import real
from nhl_edge.live import real_ledger as rl
from nhl_edge.live.exchange import cost as order_cost
from nhl_edge.live.exchange import rest
from nhl_edge.live.kalshi_trade import KalshiTrade, KalshiTradeError, OrderRequest

# The switch's settings: off, or the environment orders go to.
OFF, DEMO, LIVE = "off", kt.DEMO, kt.LIVE
# The bankroll the code's own limits are shares of (#294): twice the starting one, so real stakes
# grow with the bankroll up to there and never past it, whatever R2 holds.
CEILING_BANKROLL = 2 * real.BANKROLL
SWITCH = (OFF, DEMO, LIVE)
# An order goes only while its expiry is at least this far ahead.
LEAD = timedelta(minutes=1)
# An order still resting this long after its expiry is cancelled by the reconcile itself.
GRACE = timedelta(minutes=2)
# The record part of an earlier start a run saw in the schedule.
MOVED = "moved"
PREFIXES = {LIVE: rl.PREFIX, DEMO: "real/demo"}
NAMESPACE = uuid.UUID("5d3c1f0e-2a8b-4c6e-9f1a-7b2e4d6c8a10")
CAP_TOLERANCE = Decimal("0.0001")
NOT_MATCHED, MISSED = "not matched", "missed it"
# ADR 0045's two parts, and their record parts; ADR 0040's one order is the part "" (placed).
TAKER, MAKER = "taker", "maker"
PLACED, TAKER_PLACED, MAKER_PLACED, MAKER_SKIPPED = (
    "placed",
    "taker-placed",
    "maker-placed",
    "maker-skipped",
)
PLACED_PARTS = {"": PLACED, TAKER: TAKER_PLACED, MAKER: MAKER_PLACED}
# The status Kalshi answers a request it refuses as invalid, as a post-only bid that would cross
# the book may be.
BAD_REQUEST = 400
# A part's fill costs and fees as Kalshi's order shows them.
FILL_COSTS = (
    "taker_fill_cost_dollars",
    "taker_fees_dollars",
    "maker_fill_cost_dollars",
    "maker_fees_dollars",
)


def switch_of(value: str | None) -> str:
    """The switch's setting: off when unset, and an error for any other value."""
    setting = (value or OFF).strip().lower()
    if setting not in SWITCH:
        raise ValueError(f"KALSHI_TRADING is one of {', '.join(SWITCH)}")
    return setting


def client_order_id(env: str, row: Mapping[str, Any], part: str = "") -> str:
    """An order part's client order id: the same for the same bet's part in the same environment.
    ADR 0040's one order had no part."""
    name = f"nhl-edge:{env}:{row['mode']}:{row['game_date']}:{row['game_id']}:"
    return str(uuid.uuid5(NAMESPACE, name + row["policy_version"] + (f":{part}" if part else "")))


def team_of(row: Mapping[str, Any]) -> str:
    return row["home"] if row["side"] == "home" else row["away"]


@dataclass(frozen=True)
class Part:
    """An order part as planned: its limit in dollars, its contracts, the most it costs in USD
    with its fee, that cost in euros and its effective decimal price."""

    limit: Decimal
    contracts: Decimal
    max_cost_usd: Decimal
    stake: float
    effective: float

    @classmethod
    def of(cls, fields: Mapping[str, Any]) -> "Part":
        return cls(
            limit=Decimal(fields["limit"]),
            contracts=Decimal(fields["contracts"]),
            max_cost_usd=Decimal(fields["max_cost_usd"]),
            stake=fields["stake"],
            effective=fields["effective"],
        )

    def record(self) -> dict[str, Any]:
        return {
            "limit": f"{self.limit:.2f}",
            "contracts": f"{self.contracts:.2f}",
            "max_cost_usd": f"{self.max_cost_usd:.4f}",
            "stake": self.stake,
            "effective": self.effective,
        }


@dataclass(frozen=True)
class Plan:
    """An order to send for an alerted bet (ADR 0045): its taker part (limit, contracts,
    max_cost_usd, stake, effective, client_order_id) and its maker part (None when its stake buys
    no contract), with the client order id of each cent the maker part may rest at, from L_m down
    to L_t, and the bet's probability, hurdle and stake table, which size the maker part once the
    taker part's fills are known."""

    env: str
    game_date: date
    season: int
    game_id: int
    start_utc: datetime
    mode: str
    team: str
    ticker: str
    client_order_id: str
    limit: Decimal
    contracts: Decimal
    max_cost_usd: Decimal
    stake: float
    effective: float
    usd_per_eur: float
    bankroll: float
    expires_utc: datetime
    p_side: float
    hurdle: float
    stakes: tuple[tuple[float, float], ...]
    maker: Part | None
    maker_ids: tuple[tuple[int, str], ...]

    def record(self, written: datetime, code_version: str) -> dict[str, Any]:
        maker = None
        if self.maker is not None:
            ids = {str(cents): found for cents, found in self.maker_ids}
            maker = self.maker.record() | {"client_order_ids": ids}
        return {
            "env": self.env,
            "execution": rl.EXECUTION,
            "game_date": self.game_date.isoformat(),
            "season": self.season,
            "game_id": self.game_id,
            "start_utc": self.start_utc.isoformat(),
            "mode": self.mode,
            "team": self.team,
            "ticker": self.ticker,
            "client_order_id": self.client_order_id,
            "limit": f"{self.limit:.2f}",
            "contracts": f"{self.contracts:.2f}",
            "max_cost_usd": f"{self.max_cost_usd:.4f}",
            "stake": self.stake,
            "effective": self.effective,
            "maker": maker,
            "p_side": self.p_side,
            "hurdle": self.hurdle,
            "stakes": [list(pair) for pair in self.stakes],
            "usd_per_eur": self.usd_per_eur,
            "bankroll": self.bankroll,
            "expires_utc": self.expires_utc.isoformat(),
            "written_utc": written.isoformat(),
            "code_version": code_version,
        }

    def request(self) -> OrderRequest:
        """The taker part: immediate or cancel, so it never rests and takes no expiry."""
        return OrderRequest(
            ticker=self.ticker,
            client_order_id=self.client_order_id,
            count=self.contracts,
            price=self.limit,
            time_in_force=kt.IOC,
        )

    def maker_request(self, cents: int, contracts: Decimal) -> OrderRequest:
        """The maker part at a cent: post-only, resting until the expiry."""
        return OrderRequest(
            ticker=self.ticker,
            client_order_id=dict(self.maker_ids)[cents],
            count=contracts,
            price=Decimal(cents) / 100,
            expiration_time=self.expires_utc,
            post_only=True,
        )

    def most_cost(self) -> Decimal:
        """The most the order can cost as its row states it: the taker part's, or the maker
        part's, which the taker part's fills leave only the rest of."""
        maker = self.maker.max_cost_usd if self.maker is not None else Decimal(0)
        return max(self.max_cost_usd, maker)


def _cents(value: Decimal | str) -> int:
    return round(float(value) * 100)


def plans(rows: pl.DataFrame, setting: str) -> list[Plan]:
    """The orders the switch's setting sends for a day's alert rows, soonest game first: on live,
    each real alerted bet's order from its row; on demo, each alerted bet's order at the demo
    exchange's rules; on off, none."""
    if setting == OFF:
        return []
    found = []
    alerted = rows.filter(pl.col("status") == rl.ALERTED).sort("start_utc", "game_id")
    for row in alerted.iter_rows(named=True):
        if setting == LIVE:
            if row["mode"] != rl.REAL or row["exchange"] is None:
                continue
            order = json.loads(row["exchange"])
            if order["env"] != LIVE:
                continue
        else:
            order = rl.exchange_order(row, json.loads(row["stakes"]), real.DEMO_EXCHANGE)
            if order is None:
                continue
        team = team_of(row)
        maker = Part.of(order["maker"]) if order.get("maker") else None
        cents = range(_cents(maker.limit), _cents(order["limit"]) - 1, -1) if maker else range(0)
        found.append(
            Plan(
                env=setting,
                game_date=row["game_date"],
                season=row["season"],
                game_id=row["game_id"],
                start_utc=row["start_utc"],
                mode=row["mode"],
                team=team,
                ticker=ticker(row["game_date"], row["away"], row["home"], team),
                client_order_id=client_order_id(setting, row, TAKER),
                limit=Decimal(order["limit"]),
                contracts=Decimal(order["contracts"]),
                max_cost_usd=Decimal(order["max_cost_usd"]),
                stake=order["stake"],
                effective=order["effective"],
                usd_per_eur=order["usd_per_eur"],
                bankroll=row["bankroll"],
                expires_utc=datetime.fromisoformat(order["expires_utc"]).astimezone(UTC),
                p_side=row["p_side"],
                hurdle=row["hurdle"],
                stakes=tuple((float(p), float(s)) for p, s in json.loads(row["stakes"])),
                maker=maker,
                maker_ids=tuple((c, client_order_id(setting, row, f"{MAKER}:{c}")) for c in cents),
            )
        )
    return found


def order_key(env: str, game_date: date, game_id: int, part: str = "") -> str:
    suffix = f"-{part}" if part else ""
    return f"{PREFIXES[env]}/orders/{game_date.isoformat()}/{game_id}{suffix}.json"


def code_exchange(env: str) -> real.Exchange | None:
    """The exchange the code itself names for an environment (#294): the demo exchange for demo,
    and for live the ready venue's, once the venue commit sets it; None otherwise."""
    if env == DEMO:
        return real.DEMO_EXCHANGE
    venue = real.VENUES.get(real.VENUE) if real.VENUE is not None else None
    rules = venue.exchange if venue is not None else None
    return rules if rules is not None and rules.env == env else None


def _ceiling(share: float, rules: real.Exchange) -> Decimal:
    return Decimal(str(share * CEILING_BANKROLL * rules.usd_per_eur))


def two_parts(order: Mapping[str, Any]) -> bool:
    """Whether an order record is ADR 0045's two parts, not ADR 0040's one resting order."""
    return order.get("execution") == rl.EXECUTION


def most_cost(order: Mapping[str, Any], rules: real.Exchange) -> Decimal:
    """The most an order record can cost, recomputed from its own contracts and limits under the
    code's rules: ADR 0040's one order at the taker fee; ADR 0045's taker part at the taker fee or
    its maker part at the maker fee, whichever is more, since the maker part gets only what the
    taker part's fills leave of its own planned cost."""
    taker = order_cost(Decimal(order["contracts"]), Decimal(order["limit"]), rules)
    maker = order.get("maker") if two_parts(order) else None
    if not maker:
        return taker
    rested = order_cost(Decimal(maker["contracts"]), Decimal(maker["limit"]), rules, maker=True)
    return max(taker, rested)


def is_placed(record: Mapping[str, Any]) -> bool:
    """Whether any part of an order went to Kalshi: ADR 0040's order, or ADR 0045's taker part,
    which goes first."""
    return PLACED in record or TAKER_PLACED in record or MAKER_PLACED in record


def _sent_cost(store: lb.Store, env: str, game_date: date, rules: real.Exchange) -> Decimal:
    """The most the orders an earlier run sent for a game date can cost: each order record with a
    placed part, at its most cost recomputed from its own contracts and limits under the code's
    rules. Never from the day's alert rows, which R2 could rewrite after the orders went (Codex
    on #295)."""
    total = Decimal(0)
    for (day, _), record in stored_orders(store, env).items():
        order = record.get("order")
        if day == game_date.isoformat() and order is not None and is_placed(record):
            total += most_cost(order, rules)
    return total


def _held(store: lb.Store, key: str) -> bool:
    return key in store.keys(key.rsplit("/", 1)[0])


def _put(store: lb.Store, key: str, record: Mapping[str, Any]) -> None:
    lb.put_once(store, key, json.dumps(record, sort_keys=True).encode())


@dataclass
class PlaceReport:
    planned: int = 0
    placed: int = 0
    existed: int = 0
    already: int = 0
    failed: int = 0
    makers: int = 0
    unread: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    makers_skipped: dict[str, int] = field(default_factory=dict)
    placed_plans: list[Plan] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def skip_maker(self, reason: str) -> None:
        self.makers_skipped[reason] = self.makers_skipped.get(reason, 0) + 1

    def line(self, env: str) -> str:
        skips = "".join(f"; {n} {reason}" for reason, n in sorted(self.skipped.items()))
        makers = "".join(
            f"; {n} maker parts not sent: {reason}"
            for reason, n in sorted(self.makers_skipped.items())
        )
        unread = f"; {self.unread} maker parts waiting for their taker part" if self.unread else ""
        failed = f"; {self.failed} failed" if self.failed else ""
        return (
            f"kalshi orders {env}: {self.planned} planned, {self.placed} placed "
            f"({self.existed} found already on Kalshi), {self.already} placed before, "
            f"{self.makers} maker parts resting{skips}{makers}{unread}{failed}"
        )


def place(
    store: lb.Store,
    trade: KalshiTrade,
    planned: Sequence[Plan],
    now: Callable[[], datetime],
    code_version: str,
    starts: Mapping[int, datetime],
) -> PlaceReport:
    """Send each planned order that isn't placed yet, after the checks the module names: each is
    written to the record before it is sent, and each part's order id after. A skipped order's
    reason is written once too. starts is each game's start as the schedule lists it now: an
    order goes only while its game's start is still the alert's, so it can't rest into a game
    moved earlier. Each order's taker part goes first and its maker part right after it
    (send_maker); an order whose earlier run stopped between the two gets its maker part too."""
    report = PlaceReport(planned=len(planned))

    def skip(plan: Plan, reason: str) -> None:
        report.skip(reason)
        key = order_key(plan.env, plan.game_date, plan.game_id, "skipped")
        if not _held(store, key):
            _put(store, key, {"reason": reason, "skipped_utc": now().isoformat()})

    def held(plan: Plan, part: str) -> bool:
        return _held(store, order_key(plan.env, plan.game_date, plan.game_id, part))

    due, resumed = [], []
    for plan in planned:
        key = order_key(plan.env, plan.game_date, plan.game_id)
        if not _held(store, key):
            _put(store, key, plan.record(now(), code_version))
        if held(plan, PLACED) or held(plan, TAKER_PLACED):
            # Sent by an earlier run, final or not: it counts against the day cap (Codex on #289),
            # from its own record below. Its maker part goes now if that run stopped before it.
            report.already += 1
            if held(plan, TAKER_PLACED) and not any(
                held(plan, part) for part in (MAKER_PLACED, MAKER_SKIPPED, "final")
            ):
                resumed.append(plan)
        elif not two_parts(json.loads(store.get(key))):
            # Written by a run before ADR 0045, whose record the reconcile tracks as one order.
            skip(plan, "the order was planned before ADR 0045")
        elif held(plan, "final"):
            # Its deadline has passed once, with nothing sent: a later run never revives it.
            skip(plan, "the order is already final")
        elif plan.game_id not in starts:
            skip(plan, "the game's start couldn't be checked")
        elif starts[plan.game_id] != plan.start_utc:
            skip(plan, "the game's start moved")
        elif now() >= plan.expires_utc - LEAD:
            skip(plan, "too close to the start")
        else:
            due.append(plan)
    capped: list[Plan] = []
    code_rules: real.Exchange | None = None
    for plan in due:
        reason = _refused(plan)
        if reason is not None:
            skip(plan, reason)
        else:
            capped.append(plan)
            code_rules = code_exchange(plan.env)
    for plan in resumed:
        # The same limits for a maker part an earlier run left unsent: its row is read again now.
        reason = _refused(plan)
        if reason is not None:
            skip_maker(store, plan, reason, now, report)
        else:
            send_maker(store, trade, plan, now, starts, report)
    if capped and code_rules is not None:
        _send(store, trade, capped, code_rules, now, starts, report, skip)
    return report


def _refused(plan: Plan) -> str | None:
    """Why the code's own limits refuse an order (#294), or None: a row from R2 never sizes an
    order by itself. The exchange is the code's at the row's rate, each part costs no more than
    its row says at its own worst fee, and the order's most cost is within the code's bet ceiling
    and the bet cap."""
    rules = code_exchange(plan.env)
    if rules is None:
        return "the code names no exchange for its environment"
    if plan.usd_per_eur != rules.usd_per_eur:
        return "its rate isn't the code's"
    if order_cost(plan.contracts, plan.limit, rules) > plan.max_cost_usd + CAP_TOLERANCE or (
        plan.maker is not None
        and order_cost(plan.maker.contracts, plan.maker.limit, rules, maker=True)
        > plan.maker.max_cost_usd + CAP_TOLERANCE
    ):
        return "it costs more than its row says"
    if plan.most_cost() > _ceiling(POLICY.max_bet, rules) + CAP_TOLERANCE:
        return "over the code's bet ceiling"
    cap = Decimal(str(POLICY.max_bet * plan.bankroll * plan.usd_per_eur))
    if plan.most_cost() > cap + CAP_TOLERANCE:
        return "over the bet cap"
    return None


def _send(
    store: lb.Store,
    trade: KalshiTrade,
    capped: Sequence[Plan],
    rules: real.Exchange,
    now: Callable[[], datetime],
    starts: Mapping[int, datetime],
    report: PlaceReport,
    skip: Callable[[Plan, str], None],
) -> None:
    """The day cap and the balance for the orders that passed their own checks, then each one's
    market, its taker part and its maker part."""
    # The day's orders together, those an earlier run placed included, within the day cap and
    # within the code's own day ceiling.
    first = capped[0]
    day_cap = Decimal(str(POLICY.max_day * first.bankroll * first.usd_per_eur))
    new = sum((plan.most_cost() for plan in capped), Decimal(0))
    day_total = new + _sent_cost(store, first.env, first.game_date, rules)
    for limit, reason in (
        (_ceiling(POLICY.max_day, rules), "over the code's day ceiling"),
        (day_cap, "over the day cap"),
    ):
        if day_total > limit + CAP_TOLERANCE:
            for plan in capped:
                skip(plan, reason)
            return
    if Decimal(trade.balance()["balance"]) / 100 < new:
        for plan in capped:
            skip(plan, "the balance doesn't cover the orders")
        return
    for plan in capped:
        # A market that can't be read is its own plan's skip, never the others' (Codex on #289).
        try:
            market = trade.market(plan.ticker)
        except KalshiTradeError:
            skip(plan, "the market couldn't be read")
            continue
        if market.get("status") != "active":
            skip(plan, "the market isn't open")
            continue
        # The clock again, just before the send: the market check takes a moment.
        if now() >= plan.expires_utc - LEAD:
            skip(plan, "too close to the start")
            continue
        try:
            placed = trade.create_order(plan.request())
        except KalshiTradeError:
            # It may have reached Kalshi all the same: the reconcile finds it by its client order
            # id (adopt) before it would call it not sent. The run fails, after the others.
            report.failed += 1
            continue
        # A reconcile running meanwhile may have found it on Kalshi and recorded it first (adopt).
        with contextlib.suppress(lb.Conflict):
            _put(
                store,
                order_key(plan.env, plan.game_date, plan.game_id, TAKER_PLACED),
                {
                    "order_id": placed.order_id,
                    "client_order_id": placed.client_order_id,
                    "existed": placed.existed,
                    "placed_utc": now().isoformat(),
                },
            )
        report.placed += 1
        report.existed += placed.existed
        report.placed_plans.append(plan)
        send_maker(store, trade, plan, now, starts, report)


def _filled(order: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
    """An order's filled contracts and their cost in USD with their fees, as Kalshi shows them."""
    spent = sum((_decimal(order.get(name)) for name in FILL_COSTS), Decimal(0))
    return _decimal(order.get("fill_count_fp")), spent


def skip_maker(
    store: lb.Store, plan: Plan, reason: str, now: Callable[[], datetime], report: PlaceReport
) -> None:
    """Record once why a bet's maker part doesn't go (...-maker-skipped.json)."""
    report.skip_maker(reason)
    key = order_key(plan.env, plan.game_date, plan.game_id, MAKER_SKIPPED)
    if not _held(store, key):
        _put(store, key, {"reason": reason, "skipped_utc": now().isoformat()})


def send_maker(
    store: lb.Store,
    trade: KalshiTrade,
    plan: Plan,
    now: Callable[[], datetime],
    starts: Mapping[int, datetime],
    report: PlaceReport,
) -> None:
    """Send a bet's maker part once its taker part is placed (ADR 0045): the taker part is read
    until Kalshi shows it done, so its fills size the maker part (exchange.rest), at the code's
    rules; then a post-only bid at L_m, or a cent lower each time Kalshi refuses or cancels one at
    once for crossing the book, down to L_t. The part's placed record holds the cent it rests at,
    its contracts and most cost, the cents refused, and the taker part's fills it was sized after.
    A maker part that can't go is recorded once with its reason (...-maker-skipped.json), except
    while the taker part can't be read or the send fails, so a rerun sends it."""

    def skip(reason: str) -> None:
        skip_maker(store, plan, reason, now, report)

    rules = code_exchange(plan.env)
    if plan.maker is None:
        skip("the stake buys no contract at the maker limit")
        return
    if rules is None or plan.usd_per_eur != rules.usd_per_eur:
        skip("the code names no exchange at its rate")
        return
    if plan.game_id not in starts:
        report.unread += 1  # a rerun with the schedule sends it
        return
    if starts[plan.game_id] != plan.start_utc:
        skip("the game's start moved")
        return
    taker = json.loads(store.get(order_key(plan.env, plan.game_date, plan.game_id, TAKER_PLACED)))
    try:
        shown = trade.read_order(taker["order_id"], lambda o: o.get("status") != "resting")
    except KalshiTradeError:
        shown = None
    if shown is None or shown.get("status") == "resting":
        report.unread += 1
        return
    filled, spent = _filled(shown)
    planned = order_cost(plan.maker.contracts, plan.maker.limit, rules, maker=True)
    budget = planned - spent

    def allowed(price: float) -> float:
        return rl.stake_at(plan.stakes, price)

    step = Decimal(str(rules.step))
    crossed: list[int] = []
    for cents, _ in plan.maker_ids:
        price = Decimal(cents) / 100
        contracts = rest(price, budget, (filled, spent), plan.p_side, plan.hurdle, allowed, rules)
        if contracts < step:
            continue
        if now() >= plan.expires_utc - LEAD:
            skip("too close to the start")
            return
        try:
            sent = trade.create_order(plan.maker_request(cents, contracts))
        except KalshiTradeError as exc:
            if exc.status == BAD_REQUEST:
                crossed.append(cents)
                continue
            # It may have reached Kalshi: the reconcile finds it by its client order id.
            report.failed += 1
            return
        # A read that fails is one that lags: the part is recorded as resting, and the reconcile
        # reads it, rather than stop the other orders.
        try:
            found = trade.read_order(sent.order_id, lambda _: True)
        except KalshiTradeError:
            found = None
        if found is not None and found.get("status") != "resting" and not _filled(found)[0]:
            crossed.append(cents)  # cancelled at once: it would have crossed the book
            continue
        with contextlib.suppress(lb.Conflict):
            _put(
                store,
                order_key(plan.env, plan.game_date, plan.game_id, MAKER_PLACED),
                {
                    "order_id": sent.order_id,
                    "client_order_id": sent.client_order_id,
                    "existed": sent.existed,
                    "placed_utc": now().isoformat(),
                    "limit": f"{price:.2f}",
                    "contracts": f"{contracts:.2f}",
                    "max_cost_usd": f"{order_cost(contracts, price, rules, maker=True):.4f}",
                    "crossed": crossed,
                    "taker_contracts": f"{filled:.2f}",
                    "taker_cost_usd": f"{spent:.6f}",
                },
            )
        report.makers += 1
        return
    skip(
        "Kalshi refused it at every cent down to the taker limit"
        if crossed
        else "the taker part left no stake for it"
    )


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else Decimal(0)


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def pending_dates(store: lb.Store, env: str) -> list[date]:
    """The game dates with an order of an environment that has no final record yet."""
    return sorted(
        {
            date.fromisoformat(day)
            for (day, _), record in stored_orders(store, env).items()
            if "order" in record and "final" not in record
        }
    )


def stored_orders(store: lb.Store, env: str) -> dict[tuple[str, int], dict[str, Any]]:
    """Every order record of an environment, keyed by (game date, game id), each with its parts:
    the order, and its placed, skipped and final records when written."""
    found: dict[tuple[str, int], dict[str, Any]] = {}
    prefix = f"{PREFIXES[env]}/orders"
    for key in store.keys(prefix):
        day, name = key.removeprefix(f"{prefix}/").split("/")
        game, _, part = name.removesuffix(".json").partition("-")
        found.setdefault((day, int(game)), {})[part or "order"] = json.loads(store.get(key))
    return found


def _totals(fills: Sequence[Mapping[str, Any]]) -> tuple[Decimal, Decimal, Decimal]:
    """Fills' contracts, cost in USD with their fees, and fees."""
    contracts = sum((_decimal(f.get("count_fp")) for f in fills), Decimal(0))
    fees = sum((_decimal(f.get("fee_cost")) for f in fills), Decimal(0))
    spent = sum(
        (_decimal(f.get("count_fp")) * _decimal(f.get("yes_price_dollars")) for f in fills),
        Decimal(0),
    )
    return contracts, spent + fees, fees


def final_record(
    order: Mapping[str, Any], fills: Sequence[Mapping[str, Any]], start: datetime, read: datetime
) -> dict[str, Any]:
    """An expired order's final state: the bet, its fills before the game's start, with their
    contracts, cost in USD (fees included), fees and times; the fills at or after the start, which
    the expiry rules out, apart, and never the bet's (hard rule 2); Kalshi's order and fills as
    read. start is the earlier of the alert's start and the schedule's when it was read."""
    before = [f for f in fills if _instant(f["created_time"]) < start]
    after = [f for f in fills if _instant(f["created_time"]) >= start]
    contracts, cost, fees = _totals(before)
    late_contracts, late_cost, _ = _totals(after)
    times = sorted(_instant(f["created_time"]) for f in before)
    return {
        "status": order.get("status"),
        "start_utc": start.isoformat(),
        "contracts": f"{contracts:.2f}",
        "cost_usd": f"{cost:.6f}",
        "fees_usd": f"{fees:.6f}",
        "first_fill_utc": times[0].isoformat() if times else None,
        "last_fill_utc": times[-1].isoformat() if times else None,
        "after_start": bool(after),
        "after_start_contracts": f"{late_contracts:.2f}",
        "after_start_cost_usd": f"{late_cost:.6f}",
        "read_utc": read.isoformat(),
        "order": dict(order),
        "fills": [dict(fill) for fill in fills],
    }


def final_parts(
    parts: Mapping[str, tuple[Mapping[str, Any], Sequence[Mapping[str, Any]]]],
    start: datetime,
    read: datetime,
) -> dict[str, Any]:
    """ADR 0045's order final (final_record) from each sent part's order as read and its fills,
    taker part first: both parts' fills before the start are the bet, and the record adds each
    part's order id, status, contracts and cost before the start, Kalshi's orders as read, and
    every contract the parts filled, which Kalshi settles. order is the first part sent's, the
    taker part's, and status the maker part's when it went."""
    first = parts[TAKER] if TAKER in parts else parts[MAKER]
    fills = [fill for _, listed in parts.values() for fill in listed]
    found = final_record(first[0], fills, start, read)
    found["status"] = parts.get(MAKER, first)[0].get("status")
    found["parts"] = {}
    for name, (shown, listed) in parts.items():
        contracts, cost, _ = _totals([f for f in listed if _instant(f["created_time"]) < start])
        found["parts"][name] = {
            "order_id": shown.get("order_id"),
            "status": shown.get("status"),
            "contracts": f"{contracts:.2f}",
            "cost_usd": f"{cost:.6f}",
        }
    found["orders"] = {name: dict(shown) for name, (shown, _) in parts.items()}
    held = sum((_decimal(shown.get("fill_count_fp")) for shown, _ in parts.values()), Decimal(0))
    found["filled_contracts"] = f"{held:.2f}"
    return found


def held_contracts(final: Mapping[str, Any]) -> Decimal:
    """Every contract an order's final record filled, before the start or not, which Kalshi
    settles: ADR 0045's parts together, or ADR 0040's one order's."""
    if "filled_contracts" in final:
        return _decimal(final["filled_contracts"])
    return _decimal(final["order"].get("fill_count_fp"))


def entry(record: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """A live order's entry in the real record, as the dashboard's form writes one
    (real_ledger.ENTRY_FIELDS). Its bet id is Kalshi's order id, or for ADR 0045's order the ids
    of the parts that filled before the start, taker part first."""
    order, final = record["order"], record.get("final")
    contracts = _decimal(final.get("contracts")) if final else Decimal(0)
    base = {
        "game_date": order["game_date"],
        "game_id": order["game_id"],
        "entered_utc": now.isoformat(),
        "late": False,
    }
    if contracts <= 0 or final is None:
        reason = NOT_MATCHED if final is not None and is_placed(record) else MISSED
        return base | {
            "outcome": rl.NOT_PLACED,
            "matched_price": None,
            "stake": None,
            "bet_id": None,
            "placed_utc": None,
            "reason": reason,
        }
    cost = _decimal(final["cost_usd"])
    # A fill costing under half a cent still stakes a cent: the record needs a positive stake,
    # and the rounding is under a cent either way (Codex on #289).
    stake = max(round(float(cost) / order["usd_per_eur"], 2), 0.01)
    if "parts" in final:
        filled = [part for part in final["parts"].values() if _decimal(part["contracts"]) > 0]
        bet_id = " + ".join(str(part["order_id"]) for part in filled)
    else:
        bet_id = record[PLACED]["order_id"]
    return base | {
        "outcome": rl.PLACED,
        "matched_price": round(float(contracts / cost), 3),
        "stake": stake,
        "bet_id": bet_id,
        "placed_utc": final["first_fill_utc"],
        "reason": None,
    }


@dataclass
class ReconcileReport:
    resting: int = 0
    finalized: int = 0
    filled: int = 0
    after_start: int = 0
    cancelled: int = 0
    entries: int = 0
    waiting: int = 0
    adopted: int = 0
    finals: list[dict[str, Any]] = field(default_factory=list)

    def line(self, env: str) -> str:
        return (
            f"kalshi orders {env}: {self.resting} resting, {self.finalized} final "
            f"({self.filled} with fills, {self.after_start} filled at or after the start), "
            f"{self.cancelled} cancelled now, {self.entries} entries written, {self.waiting} "
            f"waiting for Kalshi's reads or the schedule, {self.adopted} found on Kalshi unrecorded"
        )


def _parts(order: Mapping[str, Any]) -> tuple[str, ...]:
    return (TAKER, MAKER) if two_parts(order) else ("",)


def adopt(
    store: lb.Store,
    trade: KalshiTrade,
    order: Mapping[str, Any],
    game_date: date,
    game_id: int,
    now: Callable[[], datetime],
    part: str = "",
) -> dict[str, Any] | None:
    """The placed record of an order part Kalshi holds under its client order id though no run
    recorded it, as when a create's answer was lost: written once, and returned; None when Kalshi
    has no such order. A maker part is any of its cents' ids that rests or filled: a bid Kalshi
    cancelled at once for crossing the book never held anything."""
    if part == MAKER:
        ids = {found: int(cents) for cents, found in order["maker"]["client_order_ids"].items()}
        listed = [
            found
            for found in trade.orders(order["ticker"])
            if found.get("client_order_id") in ids
            and (found.get("status") == "resting" or _decimal(found.get("fill_count_fp")) > 0)
        ]
        if len(listed) > 1:
            raise KalshiTradeError("adopt: more than one maker part rests or filled")
        found = listed[0] if listed else None
        extra = {"limit": f"{ids[found['client_order_id']] / 100:.2f}"} if found else {}
    else:
        found = trade.find_order(order["ticker"], order["client_order_id"])
        extra = {}
    if found is None:
        return None
    placed = {
        "order_id": found["order_id"],
        "client_order_id": found["client_order_id"],
        "existed": True,
        "placed_utc": now().isoformat(),
        "adopted": True,
    } | extra
    _put(store, order_key(trade.env, game_date, game_id, PLACED_PARTS[part]), placed)
    return placed


def reconcile(
    store: lb.Store,
    trade: KalshiTrade,
    now: Callable[[], datetime],
    starts: Mapping[int, datetime] | None,
    cancel: bool = False,
) -> ReconcileReport:
    """Track each order of the trade's environment that has no final record yet. Its game's start
    is the earliest of the alert's and every earlier one a run has seen in the schedule (starts,
    read now; None when it couldn't be read), each written once (...-moved-<start>.json), so one
    unread schedule loses no move. Its deadline is that start less the expiry's lead. A resting
    part is cancelled when cancel is set (the switch doesn't name its environment), once a moved
    game's deadline has passed, or when it still rests GRACE after its own expiry. Once the
    deadline has passed, each part sent is read with its fills and the order's final record
    written, and on the live exchange the bet's entry, from the fills before the start; but not in
    a run whose schedule couldn't be read (leakage audit of #281), nor while a part's fills don't
    add up to its fill count: it waits for the next run. ADR 0040's orders have one part, ADR
    0045's a taker part and a maker part."""
    report = ReconcileReport()
    env = trade.env
    held_entries = set(store.keys(f"{rl.PREFIX}/entries"))
    for (day, game_id), record in sorted(stored_orders(store, env).items()):
        if "order" not in record:
            continue
        if "final" in record:
            # A run that stopped between the final record and the entry: the entry now (Codex on
            # #289).
            key = rl.entry_key(date.fromisoformat(day), game_id)
            if env == LIVE and key not in held_entries:
                rl.store_entries(store, [entry(record, now())])
                report.entries += 1
            continue
        order = record["order"]
        start, expires = _instant(order["start_utc"]), _instant(order["expires_utc"])
        game_date = date.fromisoformat(day)
        seen = [
            _instant(part["start_utc"]) for name, part in record.items() if name.startswith(MOVED)
        ]
        listed = (starts or {}).get(game_id)
        if listed is not None and listed < start and listed not in seen:
            stamp = f"{MOVED}-{listed:%Y%m%dT%H%M%SZ}"
            _put(
                store,
                order_key(env, game_date, game_id, stamp),
                {"start_utc": listed.isoformat(), "seen_utc": now().isoformat()},
            )
            seen.append(listed)
        begins = min([start, *seen])
        deadline = min(expires, begins - (start - expires))
        sent: dict[str, dict[str, Any]] = {}
        unread = False
        for part in _parts(order):
            placed = record.get(PLACED_PARTS[part])
            # A part whose create reached Kalshi though its run never recorded it is found by its
            # client order id, at every run, so it is tracked and told while it rests, and before
            # a cancel or calling it not sent (Codex on #289). A maker part goes only after its
            # taker part, and never once its skip is written. Before those, a failed read waits
            # for the next run rather than stop this one's other orders.
            adoptable = part != MAKER or (
                TAKER in sent and bool(order.get("maker")) and MAKER_SKIPPED not in record
            )
            if placed is None and adoptable:
                try:
                    placed = adopt(store, trade, order, game_date, game_id, now, part)
                except KalshiTradeError:
                    if cancel or now() >= deadline:
                        raise
                    unread = True
                    break
                if placed is not None:
                    record = record | {PLACED_PARTS[part]: placed}
                    report.adopted += 1
            if placed is not None:
                sent[part] = placed
        if unread:
            report.waiting += 1
            continue
        if not sent:
            if now() < deadline:
                continue
            if starts is None:
                report.waiting += 1
                continue
            final: dict[str, Any] = {
                "status": "not sent",
                "reason": (record.get("skipped") or {}).get("reason"),
                "read_utc": now().isoformat(),
            }
        else:
            shown = _read_parts(trade, order, sent, cancel, now, expires, deadline, report)
            if shown is None:
                report.waiting += 1
                continue
            resting = any(found.get("status") == "resting" for found in shown.values())
            if resting or now() < deadline:
                report.resting += resting
                continue
            if starts is None:
                report.waiting += 1
                continue
            fills = {part: trade.fills(placed["order_id"]) for part, placed in sent.items()}
            if any(
                sum((_decimal(fill.get("count_fp")) for fill in fills[part]), Decimal(0))
                != _decimal(shown[part].get("fill_count_fp"))
                for part in sent
            ):
                report.waiting += 1
                continue
            if two_parts(order):
                both = {part: (shown[part], fills[part]) for part in sent}
                final = final_parts(both, begins, now())
            else:
                final = final_record(shown[""], fills[""], begins, now())
            report.filled += _decimal(final["contracts"]) > 0
            report.after_start += bool(final["after_start"])
        _put(store, order_key(env, game_date, game_id, "final"), final)
        report.finalized += 1
        report.finals.append({"order": order, "placed": sent, "final": final})
        if env == LIVE and rl.entry_key(game_date, game_id) not in held_entries:
            rl.store_entries(store, [entry(record | {"final": final}, now())])
            report.entries += 1
    return report


def _read_parts(
    trade: KalshiTrade,
    order: Mapping[str, Any],
    sent: Mapping[str, Mapping[str, Any]],
    cancel: bool,
    now: Callable[[], datetime],
    expires: datetime,
    deadline: datetime,
    report: ReconcileReport,
) -> dict[str, dict[str, Any]] | None:
    """Each sent part as Kalshi shows it, in any state, waiting a little for a new order to show
    (Kalshi's reads lag); a resting part is cancelled first when cancel is set or it is late.
    None when a part can't be read yet."""
    shown = {}
    late = now() >= (expires + GRACE if deadline == expires else deadline)
    for part, placed in sent.items():
        found = trade.read_order(placed["order_id"], lambda _: True)
        if found is None:
            return None
        if found.get("status") == "resting" and (cancel or late):
            trade.cancel(placed["order_id"], order["ticker"])
            report.cancelled += 1
            found = trade.read_order(placed["order_id"], lambda o: o.get("status") != "resting")
            if found is None:
                return None
        shown[part] = found
    return shown


def table(store: lb.Store) -> pl.DataFrame:
    """Every order of both environments (the lake's real_orders): what was planned and sent, why
    one was skipped, and its final fills. An ADR 0045 order's row is its taker part's, with its
    maker part's limit, contracts and order id beside it, and each part's contracts filled before
    the start."""
    rows = []
    for env in (LIVE, DEMO):
        for record in stored_orders(store, env).values():
            if "order" not in record:
                continue
            order, maker = record["order"], record.get(MAKER_PLACED)
            placed = record.get(PLACED) or record.get(TAKER_PLACED) or maker
            final, skipped = record.get("final"), record.get("skipped")
            parts = (final or {}).get("parts", {})
            if final is not None:
                status = final["status"]
            elif placed is not None:
                status = "placed"
            else:
                status = "skipped" if skipped else "planned"
            rows.append(
                {
                    "game_date": date.fromisoformat(order["game_date"]),
                    "season": order["season"],
                    "game_id": order["game_id"],
                    "start_utc": _instant(order["start_utc"]),
                    "env": env,
                    "mode": order["mode"],
                    "team": order["team"],
                    "ticker": order["ticker"],
                    "client_order_id": order["client_order_id"],
                    "limit": float(order["limit"]),
                    "contracts": float(order["contracts"]),
                    "max_cost_usd": float(order["max_cost_usd"]),
                    "expires_utc": _instant(order["expires_utc"]),
                    "written_utc": _instant(order["written_utc"]),
                    "status": status,
                    # A skip a later run overcame says nothing of a sent order (Codex on #289).
                    "reason": skipped["reason"] if skipped and placed is None else None,
                    "order_id": placed["order_id"] if placed else None,
                    "placed_utc": _instant(placed["placed_utc"]) if placed else None,
                    "filled": float(final["contracts"]) if final and "contracts" in final else None,
                    "cost_usd": float(final["cost_usd"]) if final and "cost_usd" in final else None,
                    "fees_usd": float(final["fees_usd"]) if final and "fees_usd" in final else None,
                    "first_fill_utc": _instant(final["first_fill_utc"])
                    if final and final.get("first_fill_utc")
                    else None,
                    "after_start": final.get("after_start") if final else None,
                    "execution": rl.EXECUTION if two_parts(order) else rl.ONE_ORDER,
                    "maker_limit": float(maker["limit"]) if maker else None,
                    "maker_contracts": float(maker["contracts"])
                    if maker and maker.get("contracts")
                    else None,
                    "maker_order_id": maker["order_id"] if maker else None,
                    "taker_filled": float(parts[TAKER]["contracts"]) if TAKER in parts else None,
                    "maker_filled": float(parts[MAKER]["contracts"]) if MAKER in parts else None,
                    "code_version": order["code_version"],
                }
            )
    columns = dtypes(RealOrders)
    built = pl.DataFrame(rows, schema=columns).sort("env", "game_date", "game_id")
    return RealOrders.validate(built)  # type: ignore[return-value]


# The owner is told of each order once it is placed and once it is final: a part's message, once
# sent, is marked (...-notified_placed.json, ...-notified_final.json), and each run tells of the
# unmarked ones, so a message Telegram refused goes with the next run (Codex on #289).
NOTIFIED = "notified"


def unnotified(store: lb.Store, env: str, part: str) -> list[dict[str, Any]]:
    """The order records with a part, placed or final, whose message hasn't gone yet, each with
    its game date and id."""
    marker = f"{NOTIFIED}_{part}"
    return [
        record | {"day": day, "game_id": game_id}
        for (day, game_id), record in sorted(stored_orders(store, env).items())
        if "order" in record
        and (is_placed(record) if part == PLACED else part in record)
        and marker not in record
    ]


def mark_notified(
    store: lb.Store, env: str, records: Sequence[Mapping[str, Any]], part: str, now: datetime
) -> None:
    """Mark each record's part as told, once."""
    for record in records:
        key = order_key(
            env, date.fromisoformat(record["day"]), record["game_id"], f"{NOTIFIED}_{part}"
        )
        if not _held(store, key):
            _put(store, key, {"notified_utc": now.isoformat()})


def _header(env: str) -> str:
    return "DEMO, mock funds" if env == DEMO else "real orders"


def placed_message(
    day: date,
    env: str,
    placed: Sequence[Mapping[str, Any]],
    skipped: Mapping[str, int] | None = None,
    failed: int = 0,
    unread: int = 0,
) -> str | None:
    """The owner's message of orders placed and not told yet (unnotified "placed"), and of the
    run's orders not placed and failed, or None without any. The alert said the bets go
    automatically, so a day whose orders all stay unsent says so too (Codex on #289). An order
    of another day than the run's says its date. An ADR 0045 order says what its taker part
    filled at once and where its maker part rests, or why it doesn't; unread counts the maker
    parts still waiting for their taker part's read."""
    if not placed and not skipped and not failed and not unread:
        return None
    lines = [f"NHL Edge · Kalshi {_header(env)} · {day:%a} {day.day} {day:%b}"]
    for record in placed:
        order = record["order"]
        expires = _instant(order["expires_utc"]).astimezone(rl.HELSINKI)
        when = "" if order["game_date"] == day.isoformat() else f"{order['game_date']} "
        size = (
            f"{order['contracts']} contracts, at most ${_decimal(order['max_cost_usd']):.2f} "
            f"({order['stake']:.2f} €)"
        )
        if not two_parts(order):
            lines.append(
                f"{when}{order['team']}: buy Yes at most {_cents(order['limit'])}¢, {size}, "
                f"until {expires:%H:%M}"
            )
            continue
        line = f"{when}{order['team']}: buy Yes at most {_cents(order['limit'])}¢ at once, {size}"
        maker, passed = record.get(MAKER_PLACED), record.get(MAKER_SKIPPED)
        if maker is not None:
            if "taker_contracts" in maker:
                line += (
                    f": {maker['taker_contracts']} filled for "
                    f"${_decimal(maker['taker_cost_usd']):.2f}"
                )
            count = f" for {maker['contracts']} contracts" if maker.get("contracts") else ""
            line += f"; then a bid at {_cents(maker['limit'])}¢{count} until {expires:%H:%M}"
        elif passed is not None:
            line += f"; no bid for the rest: {passed['reason']}"
        lines.append(line)
    if skipped:
        lines.append("Not placed: " + ", ".join(f"{n} {why}" for why, n in skipped.items()))
    if failed:
        lines.append(f"Failed to send: {failed} (the next run looks for them on Kalshi)")
    if unread:
        lines.append(
            f"Bids for the rest not sent yet: {unread}, their taker part couldn't be read (a "
            "rerun of the midday step sends them)"
        )
    return "\n".join(lines)


def fills_message(env: str, finals: Sequence[Mapping[str, Any]]) -> str | None:
    """The owner's message of orders final and not told yet (unnotified "final"), or None
    without any."""
    if not finals:
        return None
    lines = [f"NHL Edge · Kalshi {_header(env)}: fills"]
    for record in finals:
        order, final = record["order"], record["final"]
        contracts = _decimal(final.get("contracts"))
        if contracts <= 0:
            what = "nothing filled" if final["status"] != "not sent" else "no order was sent"
        else:
            cost = _decimal(final["cost_usd"])
            parts = final.get("parts", {})
            split = (
                f" ({parts[TAKER]['contracts'] if TAKER in parts else '0.00'} at once, "
                f"{parts[MAKER]['contracts'] if MAKER in parts else '0.00'} resting)"
                if two_parts(order)
                else f" of {order['contracts']}"
            )
            what = (
                f"{contracts}{split} contracts for ${cost:.2f}, fees "
                f"${_decimal(final['fees_usd']):.2f}: decimal {float(contracts / cost):.3f}, "
                f"{float(cost) / order['usd_per_eur']:.2f} €"
            )
        flag = (
            f" · {final['after_start_contracts']} contracts filled at or after the start, not in "
            "the record"
            if final.get("after_start")
            else ""
        )
        lines.append(f"{order['game_date']} {order['team']}: {what}{flag}")
    return "\n".join(lines)


# The automatic bets on the dashboard and against Kalshi's own record (#287).
ET = ZoneInfo("America/New_York")  # the NHL's game dates
KALSHI_SOURCE = "kalshi"
# Days after its game that a settlement missing on either side counts as a difference: the record
# settles once the result is public, the morning after (ADR 0003), and Kalshi soon after the game.
SETTLE_DAYS = 2
# How far back the check looks: Kalshi archives older settlements out of its live listing.
CHECK_DAYS = 30


def automatic_entries(store: lb.Store) -> list[dict[str, Any]]:
    """The live orders' entries in the real record, as the dashboard's real_entries takes them
    (#287): the form's fields, with source "kalshi" and the pipeline's own time and lateness."""
    orders = {key for key, record in stored_orders(store, LIVE).items() if "order" in record}
    return [
        {name: found.get(name) for name in rl.ENTRY_FIELDS} | {"source": KALSHI_SOURCE}
        for found in rl.entries(store)
        if (str(found["game_date"])[:10], int(found["game_id"])) in orders
    ]


@dataclass
class SettlementCheck:
    checked: int = 0
    agreed: int = 0
    waiting: int = 0
    differences: list[str] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"kalshi settlements: {self.checked} checked, {self.agreed} agree, {self.waiting} "
            f"waiting, {len(self.differences)} differences"
        )


def check_settlements(
    store: lb.Store,
    trade: KalshiTrade,
    settled: pl.DataFrame,
    today: date,
    games: pl.DataFrame,
) -> SettlementCheck:
    """Each live order with fills of the last CHECK_DAYS, against Kalshi's own settlement of its
    market (#287). Both settle on the full game (hard rule 2), so a difference goes to the owner:
    Kalshi resolving it at a fair price (scalar), another result, other contracts held than the
    order filled, or another payout; and either side missing SETTLE_DAYS after the game was
    played. games is the lake's final games, each at the start it was played: a postponed game
    counts from that start, and one neither side has settled waits while it isn't final (Codex
    on #291). The record's latest revision of each bet is the one compared.

    Kalshi's settlement is kept raw the first time it is read, at the client's clock
    (rl.write_exchange_settlement). A bet the record holds for it, one whose game was played over
    EXCHANGE_POSTPONED from its listed start or never played and resolved at a fair price, is
    then settled at Kalshi's payout by the next nhl real settle, so it waits on that first night,
    and its payout is compared after (#290)."""
    latest = {
        (row["game_date"], row["game_id"]): row
        for row in settled.sort("revision").iter_rows(named=True)
    }
    played = dict(zip(games["game_id"].to_list(), games["start_utc"].to_list(), strict=True))
    report = SettlementCheck()
    for (day, game_id), record in sorted(stored_orders(store, LIVE).items()):
        final, order = record.get("final"), record.get("order")
        game_date = date.fromisoformat(day)
        if final is None or order is None or "order" not in final:
            continue
        held = held_contracts(final)
        if held <= 0 or (today - game_date).days > CHECK_DAYS:
            continue
        report.checked += 1
        ours = latest.get((game_date, game_id))
        listed = trade.settlements(order["ticker"])
        theirs = next((s for s in listed if s.get("ticker") == order["ticker"]), None)
        name = f"{day} {order['team']}"
        new = theirs is not None and rl.write_exchange_settlement(
            store, game_date, game_id, order["ticker"], theirs, trade.now()
        )
        start = played.get(game_id)
        if start is None:
            held_for_kalshi = theirs is not None and theirs.get("market_result") == rl.SCALAR
        else:
            held_for_kalshi = abs(start - _instant(order["start_utc"])) > real.EXCHANGE_POSTPONED
        if ours is None and new and held_for_kalshi:
            report.waiting += 1  # nhl real settle pays it out at its next run
            continue
        if ours is None or theirs is None:
            if start is None and theirs is None:
                report.waiting += 1  # not played yet, as a postponed game
                continue
            # Kalshi resolving a game the lake has no final for counts from its listed date.
            since = start.astimezone(ET).date() if start is not None else game_date
            if (today - since).days >= SETTLE_DAYS:
                side = "Kalshi" if theirs is None else "the record"
                report.differences.append(f"{name}: not settled by {side} yet")
            else:
                report.waiting += 1
            continue
        found = []
        result = theirs.get("market_result")
        count = _decimal(theirs.get("yes_count_fp"))
        if ours["status"] == rl.PAYOUT:
            revenue = int(theirs.get("revenue") or 0)
            paid = (
                rl.payout_profit(ours["stake"], ours["matched_price"], revenue, float(count))
                if count > 0
                else None
            )
            if paid is None or abs(paid - ours["profit"]) > 1e-6:
                found.append(
                    f"Kalshi paid {revenue}¢ for {count} contracts; the record's payout is "
                    f"{ours['profit']:+.2f} €"
                )
        elif result not in ("yes", "no"):
            found.append(f"Kalshi resolved it as {result}, {theirs.get('value')}¢ a contract")
        elif ours["status"] != rl.SETTLED or ours["won"] != (result == "yes"):
            found.append(
                f"Kalshi: {'won' if result == 'yes' else 'lost'}; the record: "
                f"{ours['status']}, won {ours['won']}"
            )
        if count != held:
            found.append(f"Kalshi held {count} contracts, the order filled {held}")
        expected = held * 100 if result == "yes" else Decimal(0)
        paid_out = ours["status"] == rl.PAYOUT
        if not paid_out and result in ("yes", "no") and _decimal(theirs.get("revenue")) != expected:
            found.append(f"Kalshi paid {theirs.get('revenue')}¢, {expected:.0f}¢ expected")
        if found:
            report.differences.append(f"{name}: " + "; ".join(found))
        else:
            report.agreed += 1
    return report


def check_message(report: SettlementCheck) -> str | None:
    """The owner's message when the check found differences, or None."""
    if not report.differences:
        return None
    lines = ["NHL Edge · Kalshi settlements: the record and Kalshi differ"]
    lines += [f"- {difference}" for difference in report.differences]
    lines.append(
        "Check Kalshi's statement: the record's result, price or stake is corrected with nhl "
        "real correct and its reason. A game played over two weeks late, or never, is paid out "
        "at Kalshi's own price once Kalshi settles it (#290)."
    )
    return "\n".join(lines)
