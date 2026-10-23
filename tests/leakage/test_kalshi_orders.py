"""Point-in-time rules for the Kalshi orders (ADR 0040 and 0045, #281, #321), on the synthetic day
and the fake exchange: an order's two parts are built only from the day's alert, the maker part
sized after the taker part's own fills, each written before it is sent, and never from a Kalshi
price; each part clears the hurdle at the most it can pay; the maker part expires before its game,
none is sent once that is near, and a fill at or after the start never becomes a bet (hard rules
1, 2 and 4)."""

import ast
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import predict_fixtures as pf
import pytest
import real_fixtures as rf
from fakes import ConditionalBucket
from kalshi_exchange import Exchange, trade

from nhl_edge.live import bundle as lb
from nhl_edge.live import exchange as ex
from nhl_edge.live import placement as pm
from nhl_edge.live import real
from nhl_edge.live import real_ledger as rl

KALSHI = real.Venue("kalshi", 0.0, 0.01, real.Exchange("live", 1.16))


@pytest.fixture(autouse=True)
def kalshi_venue(monkeypatch: pytest.MonkeyPatch) -> None:
    """The code names Kalshi's live exchange, as the venue commit will (#294)."""
    monkeypatch.setattr(real, "VENUES", {"kalshi": KALSHI})
    monkeypatch.setattr(real, "VENUE", "kalshi")


REAL_DAY = rl.Mode(rl.REAL, rf.ALERT.date())
STARTS = {game_id: game[2] for game_id, game in pf.GAMES.items()}
# With no asks on the book, WSH's taker part is Kalshi's first order and its resting maker part
# the second.
WSH_MAKER = "order-2"


def instant(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


class Clock:
    def __init__(self) -> None:
        self.at = rf.ALERT + timedelta(minutes=3)

    def __call__(self) -> Any:
        return self.at


def rows() -> pl.DataFrame:
    return rf.alert_rows(REAL_DAY, venue=KALSHI)


def sent(tmp_path: Path, exchange: Exchange) -> tuple[lb.R2Store, list[dict[str, Any]]]:
    record, clock = lb.R2Store(ConditionalBucket(), "bucket"), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    pm.place(record, client, pm.plans(rows(), pm.LIVE), clock, rf.CODE, STARTS)
    bodies = [json.loads(r.content) for r in exchange.requests if r.method == "POST"]
    return record, bodies


def test_an_order_is_built_from_the_alert_alone_and_written_after_it(tmp_path: Path) -> None:
    alert = rows()
    record, _ = sent(tmp_path, Exchange())
    for row in alert.iter_rows(named=True):
        assert row["prediction_utc"] < row["alert_utc"]
        order = json.loads(record.get(pm.order_key("live", pf.DAY, row["game_id"])))
        assert row["alert_utc"] <= instant(order["written_utc"])
        for part in (pm.TAKER_PLACED, pm.MAKER_PLACED):
            placed = json.loads(record.get(pm.order_key("live", pf.DAY, row["game_id"], part)))
            assert instant(order["written_utc"]) <= instant(placed["placed_utc"])
        # both parts are the alert's, number for number
        expected = json.loads(row["exchange"])
        fields = ("limit", "contracts", "max_cost_usd")
        assert [order[name] for name in fields] == [expected[name] for name in fields]
        maker = {name: order["maker"][name] for name in (*fields, "stake", "effective")}
        assert maker == expected["maker"]


def test_no_kalshi_price_changes_an_order(tmp_path: Path) -> None:
    # The fake serves each market read with prices far apart; the orders sent are the same.
    low, high = Exchange(), Exchange()
    low.market_fields = {"yes_ask_dollars": "0.0500", "yes_bid_dollars": "0.0400"}
    high.market_fields = {"yes_ask_dollars": "0.9500", "yes_bid_dollars": "0.9400"}
    high.balance_cents = 10_000_000
    _, at_low = sent(tmp_path / "low", low)
    _, at_high = sent(tmp_path / "high", high)
    assert [m["yes_ask_dollars"] for m in low.markets_served] == ["0.0500"] * 3
    assert [m["yes_ask_dollars"] for m in high.markets_served] == ["0.9500"] * 3
    assert at_low == at_high and len(at_low) == 6


@pytest.mark.parametrize("asks", [[], [("0.4700", "1.20")], [("0.4900", "3.00")]])
def test_each_part_clears_the_hurdle_at_the_most_it_can_pay(
    tmp_path: Path, asks: list[tuple[str, str]]
) -> None:
    # Hard rule 4 for both parts: the taker part at the taker fee, the maker part, wherever it
    # rests between L_m and L_t and however the taker part filled, at the maker fee, the most a
    # post-only order pays; and the two together within the stake rule at their worst prices.
    exchange = Exchange()
    exchange.asks["KXNHLGAME-26OCT07PITWSH-WSH"] = list(asks)
    record, _ = sent(tmp_path, exchange)
    rules = KALSHI.exchange
    assert rules is not None
    for plan in pm.plans(rows(), pm.LIVE):
        taker = ex.cost(plan.contracts, plan.limit, rules)
        assert plan.p_side * float(plan.contracts / taker) - 1 >= plan.hurdle
        maker = json.loads(record.get(pm.order_key("live", pf.DAY, plan.game_id, pm.MAKER_PLACED)))
        price, count = Decimal(maker["limit"]), Decimal(maker["contracts"])
        assert plan.limit <= price <= plan.maker.limit  # type: ignore[union-attr]
        rested = ex.cost(count, price, rules, maker=True)
        assert plan.p_side * float(count / rested) - 1 >= plan.hurdle
        spent = Decimal(maker["taker_cost_usd"])
        filled = Decimal(maker["taker_contracts"])
        together = float((filled + count) / (spent + rested))
        assert float(spent + rested) / rules.usd_per_eur <= rl.stake_at(plan.stakes, together)


def test_every_order_expires_before_its_game_and_none_goes_once_it_is_near(
    tmp_path: Path,
) -> None:
    for plan in pm.plans(rows(), pm.LIVE):
        assert plan.expires_utc < plan.start_utc
    record, exchange, clock = lb.R2Store(ConditionalBucket(), "bucket"), Exchange(), Clock()
    clock.at = pf.START - real.Exchange("live", 1.0).expire_before - pm.LEAD
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    report = pm.place(record, client, pm.plans(rows(), pm.LIVE), clock, rf.CODE, STARTS)
    assert report.skipped == {"too close to the start": 2}
    assert all(o["ticker"].endswith("-ANA") for o in exchange.orders.values())


def test_an_order_never_rests_into_a_game_moved_earlier(tmp_path: Path) -> None:
    moved = pf.START - timedelta(hours=2)
    starts = STARTS | {2026020053: moved}
    # moved before the orders go: no order for it
    record, exchange, clock = lb.R2Store(ConditionalBucket(), "bucket"), Exchange(), Clock()
    client, _ = trade(tmp_path / "before", exchange, env=pm.LIVE, now=clock)
    report = pm.place(record, client, pm.plans(rows(), pm.LIVE), clock, rf.CODE, starts)
    assert report.skipped == {"the game's start moved": 1}
    # moved after they went: cancelled at the new deadline, and a fill after the new start is
    # never the bet's
    record, exchange, clock = lb.R2Store(ConditionalBucket(), "bucket"), Exchange(), Clock()
    client, _ = trade(tmp_path / "after", exchange, env=pm.LIVE, now=clock)
    pm.place(record, client, pm.plans(rows(), pm.LIVE), clock, rf.CODE, STARTS)
    exchange.fill(WSH_MAKER, "1.00", "0.4900", "0.0044", moved + timedelta(minutes=1))
    clock.at = moved + timedelta(minutes=10)
    pm.reconcile(record, client, clock, starts)
    assert exchange.orders[WSH_MAKER]["status"] == "canceled"
    final = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053, "final")))
    assert final["after_start"] is True and final["contracts"] == "0.00"
    wsh = next(e for e in rl.entries(record) if e["game_id"] == 2026020053)
    assert wsh["outcome"] == rl.NOT_PLACED


def test_a_move_seen_once_holds_when_the_schedule_cant_be_read_after(tmp_path: Path) -> None:
    # The leakage audit's probe: one run sees the game moved 2 hours earlier, every later run
    # has no schedule. The order is cancelled at the new deadline anyway, and nothing is final,
    # so no entry, until a run reads the schedule again; the bet is then split at the new start.
    moved = pf.START - timedelta(hours=2)
    record, exchange, clock = lb.R2Store(ConditionalBucket(), "bucket"), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    pm.place(record, client, pm.plans(rows(), pm.LIVE), clock, rf.CODE, STARTS)
    clock.at = moved - timedelta(minutes=30)
    pm.reconcile(record, client, clock, STARTS | {2026020053: moved})
    exchange.fill(WSH_MAKER, "1.00", "0.4900", "0.0044", moved + timedelta(minutes=30))
    clock.at = moved + timedelta(hours=1)
    unread = pm.reconcile(record, client, clock, None)
    assert exchange.orders[WSH_MAKER]["status"] == "canceled"
    assert unread.waiting >= 1 and not rl.entries(record)
    clock.at = pf.START + timedelta(hours=5)
    pm.reconcile(record, client, clock, None)
    assert 2026020053 not in {e["game_id"] for e in rl.entries(record)}
    pm.reconcile(record, client, clock, STARTS | {2026020053: moved})
    final = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053, "final")))
    assert final["start_utc"] == moved.isoformat() and final["contracts"] == "0.00"
    wsh = next(e for e in rl.entries(record) if e["game_id"] == 2026020053)
    assert wsh["outcome"] == rl.NOT_PLACED


# The real record and the orders are the owner's own (ADR 0039, 0040), never a model input: no
# model package or live model module imports them or names their tables.
MODEL_CODE = ("features", "game", "ratings", "lineup", "market", "betting", "backtest")
LIVE_MODEL = (
    "attribution",
    "blend_fit",
    "bundle",
    "challengers",
    "features",
    "goalies",
    "movement",
    "predict",
    "serving",
    "settle",
    "shadow",
    "slate",
    "targets",
)
PRIVATE_MODULES = ("placement", "exchange", "real_ledger", "kalshi_trade", "real", "revalue")
PRIVATE_TABLES = ("real_orders", "real_bets", "real_settlements", "real_alerts", "kalshi_books")
# The record's own R2 prefixes, read directly rather than through a module or a lake table: its
# settlements, Kalshi's settlements kept for its payouts (#290) and the demo orders.
PRIVATE_PREFIXES = ("real/live", "real/demo", "real/revalue-calls", "odds-revalue")


def imported(tree: ast.AST) -> set[str]:
    """Every module a file imports, anywhere in it, as dotted names: nhl_edge.live.placement for
    both import nhl_edge.live.placement and from nhl_edge.live import placement."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


def test_no_model_code_reads_the_real_record_or_the_orders() -> None:
    src = Path(__file__).parents[2] / "src" / "nhl_edge"
    paths = [path for package in MODEL_CODE for path in (src / package).rglob("*.py")]
    paths += [src / "live" / f"{name}.py" for name in LIVE_MODEL]
    assert all(path.exists() for path in paths)
    forbidden = {f"nhl_edge.live.{name}" for name in PRIVATE_MODULES}
    for path in paths:
        text = path.read_text()
        assert not imported(ast.parse(text)) & forbidden, path
        assert not any(f'"{table}"' in text for table in PRIVATE_TABLES), path
        assert not any(prefix in text for prefix in PRIVATE_PREFIXES), path


def test_a_fill_at_or_after_the_start_never_becomes_a_bet(tmp_path: Path) -> None:
    record, exchange, clock = lb.R2Store(ConditionalBucket(), "bucket"), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    pm.place(record, client, pm.plans(rows(), pm.LIVE), clock, rf.CODE, STARTS)
    exchange.fill(WSH_MAKER, "1.00", "0.4900", "0.0044", pf.START)
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=5)
    pm.reconcile(record, client, clock, STARTS)
    entries = rl.entries(record)
    wsh = next(e for e in entries if e["game_id"] == 2026020053)
    assert wsh["outcome"] == rl.NOT_PLACED  # its only fill came at the start
    placed = [e for e in entries if e["outcome"] == rl.PLACED]
    starts = {g: pf.GAMES[g][2] for g in pf.GAMES}
    assert all(instant(e["placed_utc"]) < starts[e["game_id"]] for e in placed)


def test_no_part_goes_once_a_run_has_seen_the_game_moved_earlier(tmp_path: Path) -> None:
    # The audit's probe (#321): a reconcile sees the game listed 2 hours earlier; the next
    # placing run's schedule lists the alert's start again. The earliest start seen holds, so
    # neither an unsent order nor a maker part its run left unsent goes, since it would rest past
    # the earlier start.
    moved = pf.START - timedelta(hours=2)
    planned = pm.plans(rows(), pm.LIVE)
    # an order never sent: its first run couldn't read the schedule
    record, exchange, clock = lb.R2Store(ConditionalBucket(), "bucket"), Exchange(), Clock()
    client, _ = trade(tmp_path / "unsent", exchange, env=pm.LIVE, now=clock)
    pm.place(record, client, planned[:1], clock, rf.CODE, {})
    pm.reconcile(record, client, clock, STARTS | {2026020053: moved})
    report = pm.place(record, client, planned[:1], clock, rf.CODE, STARTS)
    assert report.skipped == {"the game's start moved": 1} and not exchange.orders
    # a maker part its run left unsent after the taker part
    record, exchange, clock = lb.R2Store(ConditionalBucket(), "bucket"), Exchange(), Clock()
    client, _ = trade(tmp_path / "resumed", exchange, env=pm.LIVE, now=clock)
    send = pm.send_maker
    pm.send_maker = lambda *args, **kwargs: None  # type: ignore[assignment]
    try:
        pm.place(record, client, planned[:1], clock, rf.CODE, STARTS)
    finally:
        pm.send_maker = send  # type: ignore[assignment]
    pm.reconcile(record, client, clock, STARTS | {2026020053: moved})
    again = pm.place(record, client, planned[:1], clock, rf.CODE, STARTS)
    assert again.makers == 0 and again.makers_skipped == {"the game's start moved": 1}
    assert [o["body"]["time_in_force"] for o in exchange.orders.values()] == ["immediate_or_cancel"]
