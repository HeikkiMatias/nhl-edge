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
- the order expires at least LEAD from now, its game's start less the exchange's expire_before;
- the market is active;
- its most cost is within the frozen policy's max_bet of the bankroll, and the run's orders
  together within max_day;
- the balance covers them.

Each planned order is written to the record (orders/<date>/<game_id>.json) before any check, a
skipped one's reason after its check (...-skipped.json), and Kalshi's order id once it is sent
(...-placed.json). A rerun sends nothing for an order already placed.

**After its expiry** (reconcile), the order is read with its fills and written once
(...-final.json). A fill at or after its game's start, which the expiry rules out, is flagged and
gets no entry: it waits for a correction (nhl real correct). Otherwise, on the live exchange, the
bet's entry in the real record follows (real/live/entries/), as the dashboard's form would write
it:
- placed, at the matched price contracts / cost, fees included, with the stake cost / rate in
  euros, Kalshi's order id and the first fill's time;
- not placed, not matched, when nothing filled;
- not placed, missed it, when no order went.

**Logs** print counts only (run logs are public). The record holds the rest, and the owner's
Telegram gets the details.
"""

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import polars as pl

from nhl_edge.betting.selection import POLICY
from nhl_edge.ingest.kalshi import ticker
from nhl_edge.lake.schemas import RealOrders, dtypes
from nhl_edge.live import bundle as lb
from nhl_edge.live import kalshi_trade as kt
from nhl_edge.live import real
from nhl_edge.live import real_ledger as rl
from nhl_edge.live.kalshi_trade import KalshiTrade, OrderRequest

# The switch's settings: off, or the environment orders go to.
OFF, DEMO, LIVE = "off", kt.DEMO, kt.LIVE
SWITCH = (OFF, DEMO, LIVE)
# An order goes only while its expiry is at least this far ahead.
LEAD = timedelta(minutes=1)
# An order still resting this long after its expiry is cancelled by the reconcile itself.
GRACE = timedelta(minutes=2)
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
    skipped: dict[str, int] = field(default_factory=dict)
    placed_plans: list[Plan] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def line(self, env: str) -> str:
        skips = "".join(f"; {n} {reason}" for reason, n in sorted(self.skipped.items()))
        return (
            f"kalshi orders {env}: {self.planned} planned, {self.placed} placed "
            f"({self.existed} found already on Kalshi), {self.already} placed before{skips}"
        )


def place(
    store: lb.Store,
    trade: KalshiTrade,
    planned: Sequence[Plan],
    now: Callable[[], datetime],
    code_version: str,
) -> PlaceReport:
    """Send each planned order that isn't placed yet, after the checks the module names: each is
    written to the record before it is sent and its order id after. A skipped order's reason is
    written once too."""
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
            report.already += 1
            sent.append(plan)
        elif now() >= plan.expires_utc - LEAD:
            skip(plan, "too close to the start")
        else:
            due.append(plan)
    if not due:
        return report
    capped = []
    for plan in due:
        cap = Decimal(str(POLICY.max_bet * plan.bankroll * plan.usd_per_eur))
        if plan.max_cost_usd > cap + CAP_TOLERANCE:
            skip(plan, "over the bet cap")
        else:
            capped.append(plan)
    if not capped:
        return report
    # The day's orders together, those an earlier run placed included, within the day cap.
    first = capped[0]
    day_cap = Decimal(str(POLICY.max_day * first.bankroll * first.usd_per_eur))
    total = sum((plan.max_cost_usd for plan in capped), Decimal(0))
    if total + sum((plan.max_cost_usd for plan in sent), Decimal(0)) > day_cap + CAP_TOLERANCE:
        for plan in capped:
            skip(plan, "over the day cap")
        return report
    if Decimal(trade.balance()["balance"]) / 100 < total:
        for plan in capped:
            skip(plan, "the balance doesn't cover the orders")
        return report
    for plan in capped:
        market = trade.market(plan.ticker)
        if market.get("status") != "active":
            skip(plan, "the market isn't open")
            continue
        # The clock again, just before the send: the market check takes a moment.
        if now() >= plan.expires_utc - LEAD:
            skip(plan, "too close to the start")
            continue
        placed = trade.create_order(plan.request())
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


def final_record(
    order: Mapping[str, Any], fills: Sequence[Mapping[str, Any]], start: datetime, read: datetime
) -> dict[str, Any]:
    """An expired order's final state: Kalshi's order with its fills, the contracts and the total
    cost in USD, fees included, the first fill's time, and whether a fill came at or after the
    game's start."""
    contracts = _decimal(order.get("fill_count_fp"))
    fees = _decimal(order.get("taker_fees_dollars")) + _decimal(order.get("maker_fees_dollars"))
    spent = _decimal(order.get("taker_fill_cost_dollars")) + _decimal(
        order.get("maker_fill_cost_dollars")
    )
    times = sorted(_instant(fill["created_time"]) for fill in fills if fill.get("created_time"))
    return {
        "status": order.get("status"),
        "contracts": f"{contracts:.2f}",
        "cost_usd": f"{spent + fees:.6f}",
        "fees_usd": f"{fees:.6f}",
        "first_fill_utc": times[0].isoformat() if times else None,
        "last_fill_utc": times[-1].isoformat() if times else None,
        "after_start": any(time >= start for time in times),
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
    return base | {
        "outcome": rl.PLACED,
        "matched_price": round(float(contracts / cost), 3),
        "stake": round(float(cost) / order["usd_per_eur"], 2),
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
    finals: list[dict[str, Any]] = field(default_factory=list)

    def line(self, env: str) -> str:
        return (
            f"kalshi orders {env}: {self.resting} resting, {self.finalized} final "
            f"({self.filled} with fills, {self.after_start} filled at or after the start), "
            f"{self.cancelled} cancelled now, {self.entries} entries written, {self.waiting} "
            "waiting for Kalshi's reads"
        )


def reconcile(
    store: lb.Store,
    trade: KalshiTrade,
    now: Callable[[], datetime],
    cancel: bool = False,
) -> ReconcileReport:
    """Track each order of the trade's environment that has no final record yet: cancel it when
    cancel is set (the switch is off) or when it still rests GRACE after its expiry; once expired,
    read it with its fills and write its final record, and on the live exchange the bet's entry. An
    order whose fills don't add up to its fill count yet waits for the next run."""
    report = ReconcileReport()
    env = trade.env
    held_entries = set(store.keys(f"{rl.PREFIX}/entries"))
    for (day, game_id), record in sorted(stored_orders(store, env).items()):
        if "final" in record or "order" not in record:
            continue
        order = record["order"]
        start, expires = _instant(order["start_utc"]), _instant(order["expires_utc"])
        game_date = date.fromisoformat(day)
        placed = record.get("placed")
        if placed is None:
            if now() < expires:
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
            late = now() >= expires + GRACE
            if found.get("status") == "resting" and (cancel or late):
                trade.cancel(placed["order_id"], order["ticker"])
                report.cancelled += 1
                found = trade.read_order(placed["order_id"], lambda o: o.get("status") != "resting")
                if found is None:
                    report.waiting += 1
                    continue
            if found.get("status") == "resting" or now() < expires:
                report.resting += found.get("status") == "resting"
                continue
            fills = trade.fills(placed["order_id"])
            counted = sum((_decimal(fill.get("count_fp")) for fill in fills), Decimal(0))
            if counted != _decimal(found.get("fill_count_fp")):
                report.waiting += 1
                continue
            final = final_record(found, fills, start, now())
            report.filled += _decimal(final["contracts"]) > 0
            report.after_start += bool(final["after_start"])
        _put(store, order_key(env, game_date, game_id, "final"), final)
        report.finalized += 1
        report.finals.append({"order": order, "placed": placed, "final": final})
        # A fill at or after the start is no pre-game bet, and the record refuses one (hard rule
        # 2): its entry waits for a correction (nhl real correct), the owner told.
        if final.get("after_start"):
            continue
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
                    "reason": skipped["reason"] if skipped else None,
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


def placed_message(day: date, env: str, report: PlaceReport) -> str | None:
    """The owner's message after a run placed orders, or None when it placed none."""
    if not report.placed_plans:
        return None
    header = "DEMO, mock funds" if env == DEMO else "real orders"
    lines = [f"NHL Edge · Kalshi {header} · {day:%a} {day.day} {day:%b}"]
    for plan in report.placed_plans:
        expires = plan.expires_utc.astimezone(rl.HELSINKI)
        lines.append(
            f"{plan.team}: buy Yes at most {_cents(f'{plan.limit}')}¢, {plan.contracts} contracts, "
            f"at most ${plan.max_cost_usd:.2f} ({plan.stake:.2f} €), until {expires:%H:%M}"
        )
    if report.skipped:
        lines.append(
            "Not placed: " + ", ".join(f"{n} {reason}" for reason, n in report.skipped.items())
        )
    return "\n".join(lines)


def fills_message(env: str, report: ReconcileReport) -> str | None:
    """The owner's message after a run finalized orders, or None when it finalized none."""
    if not report.finals:
        return None
    header = "DEMO, mock funds" if env == DEMO else "real orders"
    lines = [f"NHL Edge · Kalshi {header}: fills"]
    for each in report.finals:
        order, final = each["order"], each["final"]
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
        flag = " · FILLED AT OR AFTER THE START" if final.get("after_start") else ""
        lines.append(f"{order['game_date']} {order['team']}: {what}{flag}")
    return "\n".join(lines)
