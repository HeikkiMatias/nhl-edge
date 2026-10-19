"""Real bets placed automatically on Kalshi (ADR 0040, #281): each alerted bet's buy order, its
fills, and its entry in the real record.

**The switch.** KALSHI_TRADING, a repository variable, is off, demo or live:
- **off:** nothing is placed, and a run cancels our resting orders;
- **demo:** every alerted bet, practice or real, goes to Kalshi's demo exchange (mock funds),
  recorded apart under real/demo/, so nothing from it enters the practice or the real record;
- **live:** the real days' alerted bets go to the live exchange, once the venue is Kalshi's
  (real.VENUE); a practice day places nothing live.

**The order** of an alerted bet is its alert row's buy order (the exchange column, written with the
day's alert) for a live order; a demo order is computed the same way at the demo exchange's rules
(real.DEMO_EXCHANGE), from the row's probability, hurdle and stake table. It goes to the team's
market (ingest/kalshi.ticker) with a client order id derived from its environment, mode, date,
game and policy, so a retry or a rerun is the same order, which Kalshi refuses twice (409).

**Before each order** (place), all of these hold, or nothing is sent and the record says why:
- the game's start, as the schedule lists it now, is still the alert's (leakage audit of #281: an
  order resting into a game moved earlier would fill during it);
- the order expires at least LEAD from now, its game's start less the exchange's expire_before;
- its most cost is within the frozen policy's max_bet of the bankroll, and the day's orders
  together, an earlier run's included, within max_day;
- **the code's own limits** (#294), whatever the alert row in R2 says: the environment's exchange
  is one the code names (DEMO_EXCHANGE, or the live venue's in real.VENUES), at the row's rate;
  the order's cost, recomputed from its contracts and limit under those rules, is no more than its
  row states; and that cost, and the day's together, are within max_bet and max_day of
  CEILING_BANKROLL at the code's rate. Anyone who could rewrite R2 could otherwise size the
  orders;
- the balance covers them;
- the market is active.

Each planned order is written to the record (orders/<date>/<game_id>.json) before any check, a
skipped one's reason after its check (...-skipped.json), and Kalshi's order id once it is sent
(...-placed.json). A rerun sends nothing for an order already placed.

**Its deadline** is its expiry, or earlier if the schedule moves its game earlier; the earliest
start any run has seen holds (...-moved-<start>.json), so one unread schedule loses no move. Each
run (reconcile) cancels a resting order whose deadline has passed. Once it has, and in a run that
read the schedule, the order is read with its fills and written once (...-final.json). The bet is
its fills before the start, the earliest seen; a fill at or after it, which the expiry rules out,
is kept apart, flagged, and never the bet's (hard rule 2). On the live exchange, the bet's entry in
the real record follows (real/live/entries/), as the dashboard's form would write it:
- placed, at the matched price contracts / cost, fees included, with the stake cost / rate in
  euros, Kalshi's order id and the first fill's time;
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


def switch_of(value: str | None) -> str:
    """The switch's setting: off when unset, and an error for any other value."""
    setting = (value or OFF).strip().lower()
    if setting not in SWITCH:
        raise ValueError(f"KALSHI_TRADING is one of {', '.join(SWITCH)}")
    return setting


def client_order_id(env: str, row: Mapping[str, Any]) -> str:
    """The order's client order id: the same for the same bet in the same environment."""
    name = f"nhl-edge:{env}:{row['mode']}:{row['game_date']}:{row['game_id']}:"
    return str(uuid.uuid5(NAMESPACE, name + row["policy_version"]))


def team_of(row: Mapping[str, Any]) -> str:
    return row["home"] if row["side"] == "home" else row["away"]


@dataclass(frozen=True)
class Plan:
    """An order to send for an alerted bet."""

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

    def record(self, written: datetime, code_version: str) -> dict[str, Any]:
        return {
            "env": self.env,
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
            "usd_per_eur": self.usd_per_eur,
            "bankroll": self.bankroll,
            "expires_utc": self.expires_utc.isoformat(),
            "written_utc": written.isoformat(),
            "code_version": code_version,
        }

    def request(self) -> OrderRequest:
        return OrderRequest(
            ticker=self.ticker,
            client_order_id=self.client_order_id,
            count=self.contracts,
            price=self.limit,
            expiration_time=self.expires_utc,
        )


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
                client_order_id=client_order_id(setting, row),
                limit=Decimal(order["limit"]),
                contracts=Decimal(order["contracts"]),
                max_cost_usd=Decimal(order["max_cost_usd"]),
                stake=order["stake"],
                effective=order["effective"],
                usd_per_eur=order["usd_per_eur"],
                bankroll=row["bankroll"],
                expires_utc=datetime.fromisoformat(order["expires_utc"]).astimezone(UTC),
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
    skipped: dict[str, int] = field(default_factory=dict)
    placed_plans: list[Plan] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def line(self, env: str) -> str:
        skips = "".join(f"; {n} {reason}" for reason, n in sorted(self.skipped.items()))
        failed = f"; {self.failed} failed" if self.failed else ""
        return (
            f"kalshi orders {env}: {self.planned} planned, {self.placed} placed "
            f"({self.existed} found already on Kalshi), {self.already} placed before{skips}"
            f"{failed}"
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
    written to the record before it is sent and its order id after. A skipped order's reason is
    written once too. starts is each game's start as the schedule lists it now: an order goes only
    while its game's start is still the alert's, so it can't rest into a game moved earlier."""
    report = PlaceReport(planned=len(planned))

    def skip(plan: Plan, reason: str) -> None:
        report.skip(reason)
        key = order_key(plan.env, plan.game_date, plan.game_id, "skipped")
        if not _held(store, key):
            _put(store, key, {"reason": reason, "skipped_utc": now().isoformat()})

    due, sent = [], []
    for plan in planned:
        key = order_key(plan.env, plan.game_date, plan.game_id)
        if not _held(store, key):
            _put(store, key, plan.record(now(), code_version))
        if _held(store, order_key(plan.env, plan.game_date, plan.game_id, "placed")):
            # Sent by an earlier run, final or not: it counts against the day cap (Codex on #289).
            report.already += 1
            sent.append(plan)
        elif _held(store, order_key(plan.env, plan.game_date, plan.game_id, "final")):
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
    if not due:
        return report
    capped, day_ceiling = [], Decimal(0)
    for plan in due:
        cap = Decimal(str(POLICY.max_bet * plan.bankroll * plan.usd_per_eur))
        # The code's own limits first (#294): a row from R2 never sizes an order by itself.
        rules = code_exchange(plan.env)
        if rules is None:
            skip(plan, "the code names no exchange for its environment")
        elif plan.usd_per_eur != rules.usd_per_eur:
            skip(plan, "its rate isn't the code's")
        elif order_cost(plan.contracts, plan.limit, rules) > plan.max_cost_usd + CAP_TOLERANCE:
            skip(plan, "it costs more than its row says")
        elif plan.max_cost_usd > _ceiling(POLICY.max_bet, rules) + CAP_TOLERANCE:
            skip(plan, "over the code's bet ceiling")
        elif plan.max_cost_usd > cap + CAP_TOLERANCE:
            skip(plan, "over the bet cap")
        else:
            capped.append(plan)
            day_ceiling = _ceiling(POLICY.max_day, rules)
    if not capped:
        return report
    # The day's orders together, those an earlier run placed included, within the day cap and
    # within the code's own day ceiling.
    first = capped[0]
    day_cap = Decimal(str(POLICY.max_day * first.bankroll * first.usd_per_eur))
    new = sum((plan.max_cost_usd for plan in capped), Decimal(0))
    day_total = new + sum((plan.max_cost_usd for plan in sent), Decimal(0))
    for limit, reason in (
        (day_ceiling, "over the code's day ceiling"),
        (day_cap, "over the day cap"),
    ):
        if day_total > limit + CAP_TOLERANCE:
            for plan in capped:
                skip(plan, reason)
            return report
    if Decimal(trade.balance()["balance"]) / 100 < new:
        for plan in capped:
            skip(plan, "the balance doesn't cover the orders")
        return report
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
                order_key(plan.env, plan.game_date, plan.game_id, "placed"),
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
    return report


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


def entry(record: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """A live order's entry in the real record, as the dashboard's form writes one
    (real_ledger.ENTRY_FIELDS)."""
    order, final = record["order"], record.get("final")
    contracts = _decimal(final.get("contracts")) if final else Decimal(0)
    base = {
        "game_date": order["game_date"],
        "game_id": order["game_id"],
        "entered_utc": now.isoformat(),
        "late": False,
    }
    if contracts <= 0 or final is None:
        reason = NOT_MATCHED if final is not None and "placed" in record else MISSED
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
    return base | {
        "outcome": rl.PLACED,
        "matched_price": round(float(contracts / cost), 3),
        "stake": stake,
        "bet_id": record["placed"]["order_id"],
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


def adopt(
    store: lb.Store,
    trade: KalshiTrade,
    order: Mapping[str, Any],
    game_date: date,
    game_id: int,
    now: Callable[[], datetime],
) -> dict[str, Any] | None:
    """The placed record of an order Kalshi holds under its client order id though no run recorded
    it, as when a create's answer was lost: written once, and returned; None when Kalshi has no
    such order."""
    found = trade.find_order(order["ticker"], order["client_order_id"])
    if found is None:
        return None
    placed = {
        "order_id": found["order_id"],
        "client_order_id": order["client_order_id"],
        "existed": True,
        "placed_utc": now().isoformat(),
        "adopted": True,
    }
    _put(store, order_key(trade.env, game_date, game_id, "placed"), placed)
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
    order is cancelled when cancel is set (the switch doesn't name its environment), once a moved
    game's deadline has passed, or when it still rests GRACE after its own expiry. Once the
    deadline has passed, a placed order is read with its fills and its final record written, and
    on the live exchange the bet's entry, from the fills before the start; but not in a run whose
    schedule couldn't be read (leakage audit of #281), nor while its fills don't add up to its fill
    count: it waits for the next run."""
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
        placed = record.get("placed")
        if placed is None:
            # An order whose create reached Kalshi though its run never recorded it is found by
            # its client order id, at every run, so it is tracked and told while it rests, and
            # before a cancel or calling it not sent (Codex on #289). Before those, a failed read
            # waits for the next run rather than stop this one's other orders.
            try:
                placed = adopt(store, trade, order, game_date, game_id, now)
            except KalshiTradeError:
                if cancel or now() >= deadline:
                    raise
                report.waiting += 1
                continue
            if placed is not None:
                record = record | {"placed": placed}
                report.adopted += 1
        if placed is None:
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
            # Any state, waiting a little for a new order to show (Kalshi's reads lag).
            found = trade.read_order(placed["order_id"], lambda _: True)
            if found is None:
                report.waiting += 1
                continue
            late = now() >= (expires + GRACE if deadline == expires else deadline)
            if found.get("status") == "resting" and (cancel or late):
                trade.cancel(placed["order_id"], order["ticker"])
                report.cancelled += 1
                found = trade.read_order(placed["order_id"], lambda o: o.get("status") != "resting")
                if found is None:
                    report.waiting += 1
                    continue
            if found.get("status") == "resting" or now() < deadline:
                report.resting += found.get("status") == "resting"
                continue
            if starts is None:
                report.waiting += 1
                continue
            fills = trade.fills(placed["order_id"])
            counted = sum((_decimal(fill.get("count_fp")) for fill in fills), Decimal(0))
            if counted != _decimal(found.get("fill_count_fp")):
                report.waiting += 1
                continue
            final = final_record(found, fills, begins, now())
            report.filled += _decimal(final["contracts"]) > 0
            report.after_start += bool(final["after_start"])
        _put(store, order_key(env, game_date, game_id, "final"), final)
        report.finalized += 1
        report.finals.append({"order": order, "placed": placed, "final": final})
        if env == LIVE and rl.entry_key(game_date, game_id) not in held_entries:
            rl.store_entries(store, [entry(record | {"final": final}, now())])
            report.entries += 1
    return report


def table(store: lb.Store) -> pl.DataFrame:
    """Every order of both environments (the lake's real_orders): what was planned and sent, why
    one was skipped, and its final fills."""
    rows = []
    for env in (LIVE, DEMO):
        for record in stored_orders(store, env).values():
            if "order" not in record:
                continue
            order, placed = record["order"], record.get("placed")
            final, skipped = record.get("final"), record.get("skipped")
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
                    "code_version": order["code_version"],
                }
            )
    columns = dtypes(RealOrders)
    built = pl.DataFrame(rows, schema=columns).sort("env", "game_date", "game_id")
    return RealOrders.validate(built)  # type: ignore[return-value]


def _cents(value: str) -> int:
    return round(float(value) * 100)


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
        if "order" in record and part in record and marker not in record
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
) -> str | None:
    """The owner's message of orders placed and not told yet (unnotified "placed"), and of the
    run's orders not placed and failed, or None without any. The alert said the bets go
    automatically, so a day whose orders all stay unsent says so too (Codex on #289). An order
    of another day than the run's says its date."""
    if not placed and not skipped and not failed:
        return None
    lines = [f"NHL Edge · Kalshi {_header(env)} · {day:%a} {day.day} {day:%b}"]
    for record in placed:
        order = record["order"]
        expires = _instant(order["expires_utc"]).astimezone(rl.HELSINKI)
        when = "" if order["game_date"] == day.isoformat() else f"{order['game_date']} "
        lines.append(
            f"{when}{order['team']}: buy Yes at most {_cents(order['limit'])}¢, "
            f"{order['contracts']} contracts, at most ${_decimal(order['max_cost_usd']):.2f} "
            f"({order['stake']:.2f} €), until {expires:%H:%M}"
        )
    if skipped:
        lines.append("Not placed: " + ", ".join(f"{n} {why}" for why, n in skipped.items()))
    if failed:
        lines.append(f"Failed to send: {failed} (the next run looks for them on Kalshi)")
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
            what = (
                f"{contracts} of {order['contracts']} contracts for ${cost:.2f}, fees "
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
    on #291). The record's latest revision of each bet is the one compared."""
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
        held = _decimal(final["order"].get("fill_count_fp"))
        if held <= 0 or (today - game_date).days > CHECK_DAYS:
            continue
        report.checked += 1
        ours = latest.get((game_date, game_id))
        listed = trade.settlements(order["ticker"])
        theirs = next((s for s in listed if s.get("ticker") == order["ticker"]), None)
        name = f"{day} {order['team']}"
        if ours is None or theirs is None:
            start = played.get(game_id)
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
        if result not in ("yes", "no"):
            found.append(f"Kalshi resolved it as {result}, {theirs.get('value')}¢ a contract")
        elif ours["status"] != rl.SETTLED or ours["won"] != (result == "yes"):
            found.append(
                f"Kalshi: {'won' if result == 'yes' else 'lost'}; the record: "
                f"{ours['status']}, won {ours['won']}"
            )
        count = _decimal(theirs.get("yes_count_fp"))
        if count != held:
            found.append(f"Kalshi held {count} contracts, the order filled {held}")
        expected = held * 100 if result == "yes" else Decimal(0)
        if result in ("yes", "no") and _decimal(theirs.get("revenue")) != expected:
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
        "real correct and its reason; a game Kalshi resolved at its own price waits for #290."
    )
    return "\n".join(lines)
