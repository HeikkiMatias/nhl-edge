"""Real bets placed automatically on Kalshi (ADR 0040, #281), against the fake exchange: the
switch, the orders and their checks, never twice, the fills, the entries and the record. Every
response is synthetic and no test calls Kalshi."""

import dataclasses
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import predict_fixtures as pf
import pytest
import real_fixtures as rf
from fakes import ConditionalBucket
from kalshi_exchange import Exchange, trade
from typer.testing import CliRunner

from nhl_edge.cli import app
from nhl_edge.lake.schemas import RealOrders
from nhl_edge.live import bundle as lb
from nhl_edge.live import kalshi_trade as kt
from nhl_edge.live import placement as pm
from nhl_edge.live import real
from nhl_edge.live import real_ledger as rl
from nhl_edge.live.kalshi_trade import KalshiTradeError

KALSHI = real.Venue("kalshi", 0.0, 0.01, real.Exchange("live", 1.16))
REAL_DAY = rl.Mode(rl.REAL, rf.ALERT.date())
PLACED_AT = rf.ALERT + timedelta(minutes=3)
EXPIRES = pf.START - timedelta(minutes=5)
WSH = "KXNHLGAME-26OCT07PITWSH-WSH"
# Each game's start as the schedule lists it when the orders go: the alert's, and the made-up
# games the cap tests add.
STARTS = {game_id: game[2] for game_id, game in pf.GAMES.items()} | {
    2026020098: pf.START,
    2026020099: pf.START,
}


class Clock:
    def __init__(self, at: datetime = PLACED_AT) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def real_rows() -> pl.DataFrame:
    return rf.alert_rows(REAL_DAY, venue=KALSHI)


def store() -> lb.R2Store:
    return lb.R2Store(ConditionalBucket(), "bucket")


def placed(
    tmp_path: Path, rows: pl.DataFrame | None = None, setting: str = pm.LIVE
) -> tuple[lb.R2Store, Exchange, kt.KalshiTrade, Clock, pm.PlaceReport]:
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=setting, now=clock)
    planned = pm.plans(real_rows() if rows is None else rows, setting)
    report = pm.place(record, client, planned, clock, rf.CODE, STARTS)
    return record, exchange, client, clock, report


# The switch and the plans


def test_the_switch_is_off_unless_set() -> None:
    assert pm.switch_of(None) == pm.switch_of("") == pm.OFF
    assert pm.switch_of(" Live ") == pm.LIVE and pm.switch_of("demo") == pm.DEMO
    with pytest.raises(ValueError, match="KALSHI_TRADING is one of off, demo, live"):
        pm.switch_of("on")


def test_off_plans_nothing_and_live_only_the_real_days_bets() -> None:
    assert pm.plans(real_rows(), pm.OFF) == []
    assert pm.plans(rf.alert_rows(), pm.LIVE) == []  # a practice day places nothing live
    planned = pm.plans(real_rows(), pm.LIVE)
    assert [p.game_id for p in planned] == [2026020053, 2026020054, 2026020055]
    first = planned[0]
    order = json.loads(real_rows().filter(pl.col("game_id") == 2026020053)["exchange"].item())
    assert (first.env, first.mode, first.team, first.ticker) == ("live", "real", "WSH", WSH)
    assert first.limit == Decimal(order["limit"]) and first.contracts == Decimal(order["contracts"])
    assert first.expires_utc == EXPIRES and first.bankroll == 100.0


def test_demo_plans_every_alerted_bet_at_the_demo_exchanges_rules() -> None:
    planned = pm.plans(rf.alert_rows(), pm.DEMO)
    assert [(p.env, p.mode) for p in planned] == [("demo", "practice")] * 3
    assert planned[0].usd_per_eur == real.DEMO_EXCHANGE.usd_per_eur
    assert [p.game_id for p in pm.plans(real_rows(), pm.DEMO)] == [p.game_id for p in planned]


def test_a_bets_client_order_id_is_the_same_on_every_run_and_its_own() -> None:
    live = pm.plans(real_rows(), pm.LIVE)
    again = pm.plans(real_rows(), pm.LIVE)
    demo = pm.plans(real_rows(), pm.DEMO)
    assert [p.client_order_id for p in live] == [p.client_order_id for p in again]
    ids = {p.client_order_id for p in live} | {p.client_order_id for p in demo}
    assert len(ids) == 6


def test_a_skipped_bet_is_never_planned() -> None:
    rows = real_rows().with_columns(
        status=pl.when(pl.col("game_id") == 2026020054)
        .then(pl.lit(rl.SKIP_STARTED))
        .otherwise(pl.col("status")),
        exchange=pl.when(pl.col("game_id") == 2026020054).then(None).otherwise(pl.col("exchange")),
    )
    assert [p.game_id for p in pm.plans(rows, pm.LIVE)] == [2026020053, 2026020055]


# Placing


def test_each_order_is_written_before_it_is_sent_and_expires_before_its_game(
    tmp_path: Path,
) -> None:
    record, exchange, _, _, report = placed(tmp_path)
    assert report.placed == 3 and not report.skipped
    creates = [json.loads(r.content) for r in exchange.requests if r.method == "POST"]
    first = pm.plans(real_rows(), pm.LIVE)[0]
    assert creates[0] == {
        "ticker": WSH,
        "client_order_id": first.client_order_id,
        "side": "bid",
        "count": f"{first.contracts:.2f}",
        "price": f"{first.limit:.4f}",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "cancel_order_on_pause": True,
        "expiration_time": int(EXPIRES.timestamp()),
    }
    written = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053)))
    sent = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053, "placed")))
    assert (
        written["client_order_id"] == first.client_order_id and written["code_version"] == rf.CODE
    )
    assert sent == {
        "order_id": "order-1",
        "client_order_id": first.client_order_id,
        "existed": False,
        "placed_utc": PLACED_AT.isoformat(),
    }


def test_the_record_is_written_before_the_create_reaches_kalshi(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    seen: list[bool] = []
    create = exchange._create

    def checked(body: dict[str, Any]) -> Any:
        keys = record.keys(f"{rl.PREFIX}/orders")
        seen.append(any(body["client_order_id"] in record.get(k).decode() for k in keys))
        return create(body)

    exchange._create = checked  # type: ignore[method-assign]
    pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, STARTS)
    assert seen == [True, True, True]


def test_a_rerun_sends_nothing_for_an_order_already_placed(tmp_path: Path) -> None:
    record, exchange, client, clock, _ = placed(tmp_path)
    again = pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, STARTS)
    assert again.already == 3 and again.placed == 0
    assert len(exchange.orders) == 3


def test_a_create_that_reached_kalshi_before_failing_is_found_on_the_rerun(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    planned = pm.plans(real_rows(), pm.LIVE)[:1]
    create = exchange._create
    tries = {"n": 0}

    def failing(body: dict[str, Any]) -> Any:
        # The first try reaches Kalshi but its answer is lost; the later ones fail outright.
        tries["n"] += 1
        if tries["n"] == 1:
            create(body)
        return httpx.Response(502, json={"error": "synthetic"})

    exchange._create = failing  # type: ignore[method-assign]
    with pytest.raises(KalshiTradeError):
        pm.place(record, client, planned, clock, rf.CODE, STARTS)
    assert len(exchange.orders) == 1 and tries["n"] == 5
    exchange._create = create  # type: ignore[method-assign]
    again = pm.place(record, client, planned, clock, rf.CODE, STARTS)
    assert again.placed == 1 and again.existed == 1 and len(exchange.orders) == 1


def test_an_order_too_close_to_its_expiry_is_skipped(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock(EXPIRES - timedelta(seconds=30))
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    report = pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, STARTS)
    # the two games at 23:30 UTC are skipped, the one at 02:00 goes
    assert report.placed == 1 and report.skipped == {"too close to the start": 2}
    assert [o["ticker"] for o in exchange.orders.values()] == ["KXNHLGAME-26OCT07EDMANA-ANA"]
    skipped = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053, "skipped")))
    assert skipped["reason"] == "too close to the start"


def test_a_market_that_isnt_open_is_skipped(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    exchange.status[WSH] = "closed"
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    report = pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, STARTS)
    assert report.placed == 2 and report.skipped == {"the market isn't open": 1}


def test_an_order_over_the_bet_cap_is_never_sent(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    planned = pm.plans(real_rows(), pm.LIVE)
    over = dataclasses.replace(planned[0], max_cost_usd=Decimal("1.7500"))  # 1.5% is $1.74
    report = pm.place(record, client, [over, *planned[1:]], clock, rf.CODE, STARTS)
    assert report.skipped == {"over the bet cap": 1} and report.placed == 2


def test_orders_over_the_day_cap_are_never_sent(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    planned = pm.plans(real_rows(), pm.LIVE)
    # 5% of 100 € at 1.16 is $5.80; four orders of $1.70 each go over it
    big = [dataclasses.replace(p, max_cost_usd=Decimal("1.70")) for p in planned]
    extra = dataclasses.replace(big[0], game_id=2026020099, client_order_id="other")
    report = pm.place(record, client, [*big, extra], clock, rf.CODE, STARTS)
    assert report.skipped == {"over the day cap": 4} and not exchange.orders


def test_the_day_cap_counts_the_orders_an_earlier_run_placed(tmp_path: Path) -> None:
    record, exchange, client, clock, _ = placed(tmp_path)
    planned = pm.plans(real_rows(), pm.LIVE)
    late = [
        dataclasses.replace(
            planned[0], game_id=game_id, client_order_id=f"late-{game_id}", max_cost_usd=cost
        )
        for game_id, cost in ((2026020098, Decimal("1.74")), (2026020099, Decimal("1.74")))
    ]
    report = pm.place(record, client, [*planned, *late], clock, rf.CODE, STARTS)
    # the two new ones alone, $3.48, are within $5.80; with the three placed before they aren't
    assert sum(p.max_cost_usd for p in planned) + Decimal("3.48") > Decimal("5.80")
    assert report.skipped == {"over the day cap": 2} and len(exchange.orders) == 3


def test_orders_the_balance_doesnt_cover_are_never_sent(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    exchange.balance_cents = 100
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    report = pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, STARTS)
    assert report.skipped == {"the balance doesn't cover the orders": 3} and not exchange.orders


# Fills and entries


def finish(
    tmp_path: Path, fills: list[tuple[str, str, str, datetime]] | None = None
) -> tuple[lb.R2Store, Exchange, kt.KalshiTrade, Clock]:
    """The day's live orders placed, the first one filled as given, all expired."""
    record, exchange, client, clock, _ = placed(tmp_path)
    for count, price, fee, at in fills or []:
        exchange.fill("order-1", count, price, fee, at)
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=4)
    return record, exchange, client, clock


def test_an_order_is_tracked_while_it_rests(tmp_path: Path) -> None:
    record, _, client, clock, _ = placed(tmp_path)
    clock.at = EXPIRES - timedelta(minutes=30)
    report = pm.reconcile(record, client, clock, STARTS)
    assert report.resting == 3 and report.finalized == 0
    assert not record.keys(f"{rl.PREFIX}/entries")


def test_a_filled_order_becomes_its_bets_entry(tmp_path: Path) -> None:
    first_fill = PLACED_AT + timedelta(seconds=2)
    record, _, client, clock = finish(
        tmp_path,
        [
            ("1.00", "0.4700", "0.017500", first_fill),
            ("0.50", "0.4600", "0.008700", PLACED_AT + timedelta(hours=1)),
        ],
    )
    report = pm.reconcile(record, client, clock, STARTS)
    assert report.finalized == 3 and report.filled == 1 and report.entries == 3
    final = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053, "final")))
    assert final["contracts"] == "1.50" and final["after_start"] is False
    cost = Decimal("1.00") * Decimal("0.47") + Decimal("0.50") * Decimal("0.46") + Decimal("0.0262")
    assert Decimal(final["cost_usd"]) == cost and final["first_fill_utc"] == first_fill.isoformat()
    entries = {e["game_id"]: e for e in rl.entries(record)}
    assert entries[2026020053] | {"entered_utc": None} == {
        "game_date": pf.DAY.isoformat(),
        "game_id": 2026020053,
        "outcome": "placed",
        "matched_price": round(float(Decimal("1.50") / cost), 3),
        "stake": round(float(cost) / 1.16, 2),
        "bet_id": "order-1",
        "placed_utc": first_fill.isoformat(),
        "reason": None,
        "entered_utc": None,
        "late": False,
    }
    assert entries[2026020054]["outcome"] == rl.NOT_PLACED
    assert entries[2026020054]["reason"] == pm.NOT_MATCHED
    # the record takes them, within the stake rule
    bets = rl.bets(real_rows(), rl.entries(record))
    wsh = bets.filter(pl.col("game_id") == 2026020053).row(0, named=True)
    assert wsh["outcome"] == rl.PLACED and wsh["within_rule"] is True and wsh["late"] is False


def test_an_order_never_sent_is_missed_once_it_would_have_expired(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    exchange.status[WSH] = "closed"
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, STARTS)
    clock.at = pf.START + timedelta(hours=4)
    for order_id in exchange.orders:
        exchange.expire(order_id)
    pm.reconcile(record, client, clock, STARTS)
    final = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053, "final")))
    assert final["status"] == "not sent" and final["reason"] == "the market isn't open"
    wsh = next(e for e in rl.entries(record) if e["game_id"] == 2026020053)
    assert wsh["outcome"] == rl.NOT_PLACED and wsh["reason"] == pm.MISSED


def test_a_fill_at_the_start_is_kept_apart_and_never_the_bets(tmp_path: Path) -> None:
    record, _, client, clock = finish(tmp_path, [("1.00", "0.4700", "0.017500", pf.START)])
    report = pm.reconcile(record, client, clock, STARTS)
    assert report.after_start == 1 and report.entries == 3
    final = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053, "final")))
    assert final["after_start"] is True and final["contracts"] == "0.00"
    assert final["after_start_contracts"] == "1.00"
    wsh = next(e for e in rl.entries(record) if e["game_id"] == 2026020053)
    assert wsh["outcome"] == rl.NOT_PLACED and wsh["reason"] == pm.NOT_MATCHED
    assert "1.00 contracts filled at or after the start, not in the record" in str(
        pm.fills_message(pm.LIVE, report)
    )


def test_only_the_fills_before_the_start_are_the_bet(tmp_path: Path) -> None:
    before = pf.START - timedelta(hours=1)
    record, _, client, clock = finish(
        tmp_path,
        [
            ("1.00", "0.4700", "0.017500", before),
            ("0.50", "0.4500", "0.008700", pf.START + timedelta(minutes=1)),
        ],
    )
    pm.reconcile(record, client, clock, STARTS)
    wsh = next(e for e in rl.entries(record) if e["game_id"] == 2026020053)
    cost = Decimal("0.47") + Decimal("0.0175")
    assert wsh["outcome"] == rl.PLACED and wsh["placed_utc"] == before.isoformat()
    assert wsh["matched_price"] == round(float(1 / cost), 3)
    assert wsh["stake"] == round(float(cost) / 1.16, 2)


def test_a_game_whose_start_moved_or_isnt_listed_gets_no_order(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    starts = STARTS | {2026020053: pf.START - timedelta(hours=1)}
    starts.pop(2026020054)
    report = pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, starts)
    assert report.placed == 1 and report.skipped == {
        "the game's start moved": 1,
        "the game's start couldn't be checked": 1,
    }
    assert [o["ticker"] for o in exchange.orders.values()] == ["KXNHLGAME-26OCT07EDMANA-ANA"]


def test_a_resting_order_of_a_game_moved_earlier_is_cancelled_at_its_new_deadline(
    tmp_path: Path,
) -> None:
    record, exchange, client, clock, _ = placed(tmp_path)
    moved = pf.START - timedelta(hours=2)  # 21:30 UTC, before the order's own expiry
    starts = STARTS | {2026020053: moved}
    clock.at = moved - timedelta(minutes=10)
    assert pm.reconcile(record, client, clock, starts).cancelled == 0
    exchange.fill("order-1", "1.00", "0.4700", "0.017500", moved - timedelta(minutes=20))
    clock.at = moved - timedelta(minutes=4)  # past the new deadline, 5 minutes before
    report = pm.reconcile(record, client, clock, starts)
    assert report.cancelled == 1 and report.finalized == 1
    assert exchange.orders["order-1"]["status"] == "canceled"
    final = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053, "final")))
    assert final["start_utc"] == moved.isoformat() and final["contracts"] == "1.00"


def test_fills_not_yet_listed_wait_for_the_next_run(tmp_path: Path) -> None:
    record, exchange, client, clock, _ = placed(tmp_path)
    exchange.fill("order-1", "1.00", "0.4700", "0.017500", PLACED_AT, record=False)
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=4)
    report = pm.reconcile(record, client, clock, STARTS)
    assert report.waiting == 1 and report.finalized == 2
    exchange.fills["order-1"] = [
        {
            "count_fp": "1.00",
            "yes_price_dollars": "0.4700",
            "created_time": PLACED_AT.isoformat(),
            "fee_cost": "0.0175",
        }
    ]
    later = pm.reconcile(record, client, clock, STARTS)
    assert later.finalized == 1 and later.entries == 1


def test_a_final_record_is_written_once(tmp_path: Path) -> None:
    record, _, client, clock = finish(tmp_path)
    assert pm.reconcile(record, client, clock, STARTS).finalized == 3
    again = pm.reconcile(record, client, clock, STARTS)
    assert again.finalized == 0 and again.entries == 0


def test_the_switch_off_cancels_resting_orders(tmp_path: Path) -> None:
    record, exchange, client, clock, _ = placed(tmp_path)
    clock.at = EXPIRES - timedelta(hours=1)
    report = pm.reconcile(record, client, clock, STARTS, cancel=True)
    assert report.cancelled == 3
    assert {o["status"] for o in exchange.orders.values()} == {"canceled"}
    # each is final once its expiry has passed, unfilled
    clock.at = pf.START + timedelta(hours=4)
    assert pm.reconcile(record, client, clock, STARTS).finalized == 3


def test_an_order_still_resting_after_its_expiry_is_cancelled(tmp_path: Path) -> None:
    record, _, client, clock, _ = placed(tmp_path)
    clock.at = EXPIRES + pm.GRACE
    report = pm.reconcile(record, client, clock, STARTS)
    # the two 23:30 games' orders; the 02:00 game's still rests before its own expiry
    assert report.cancelled == 2 and report.finalized == 2 and report.resting == 1


def test_demo_orders_are_kept_apart_and_never_enter_the_record(tmp_path: Path) -> None:
    record, exchange, client, clock, report = placed(tmp_path, rf.alert_rows(), pm.DEMO)
    assert report.placed == 3
    assert record.keys("real/demo/orders") and not record.keys(f"{rl.PREFIX}/orders")
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=4)
    assert pm.reconcile(record, client, clock, STARTS).entries == 0
    assert not record.keys(f"{rl.PREFIX}/entries")


# The table and the messages


def test_the_orders_table_holds_every_state(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    exchange.status[WSH] = "closed"
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, STARTS)
    exchange.fill("order-1", "1.00", "0.4700", "0.017500", PLACED_AT)
    built = pm.table(record)
    RealOrders.validate(built)
    assert built["status"].to_list() == ["skipped", "placed", "placed"]
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=4)
    pm.reconcile(record, client, clock, STARTS)
    final = pm.table(record)
    assert final["status"].to_list() == ["not sent", "canceled", "canceled"]
    assert final["filled"].to_list() == [None, 1.0, 0.0]


def test_the_messages_give_the_orders_and_the_fills(tmp_path: Path) -> None:
    record, exchange, client, clock, report = placed(tmp_path)
    text = pm.placed_message(pf.DAY, pm.LIVE, report)
    first = pm.plans(real_rows(), pm.LIVE)[0]
    assert text is not None and text.startswith("NHL Edge · Kalshi real orders · Wed 7 Oct")
    assert f"WSH: buy Yes at most {round(first.limit * 100)}¢, {first.contracts} contracts" in text
    assert pm.placed_message(pf.DAY, pm.LIVE, pm.PlaceReport()) is None
    exchange.fill("order-1", "1.00", "0.4700", "0.017500", PLACED_AT)
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=4)
    fills = pm.fills_message(pm.LIVE, pm.reconcile(record, client, clock, STARTS))
    assert fills is not None and "2026-10-07 WSH: 1.00 of" in fills
    assert "2026-10-07 WPG: nothing filled" in fills


def test_a_demo_message_says_so(tmp_path: Path) -> None:
    *_, report = placed(tmp_path, rf.alert_rows(), pm.DEMO)
    text = pm.placed_message(pf.DAY, pm.DEMO, report)
    assert text is not None and "Kalshi DEMO, mock funds" in text


# The commands


runner = CliRunner()


def commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str | None, rows: pl.DataFrame
) -> tuple[lb.R2Store, Exchange, list[str], Any]:
    """The CLI wired to an in-memory record holding the day's alert, a fake exchange and a
    message catcher, at the day's alert plus three minutes."""
    from nhl_edge import cli
    from nhl_edge.live import telegram

    record, exchange, clock = store(), Exchange(), Clock()
    record.put(rl.alerts_key(pf.DAY), json.dumps({"rows": rl.jsonable(rows)}).encode())

    class FakeLake:
        def __init__(self) -> None:
            self.replaced: list[tuple[str, int]] = []

        def replace(self, table: str, frame: pl.DataFrame) -> None:
            self.replaced.append((table, frame.height))

    lake, sent = FakeLake(), []
    monkeypatch.setattr(cli, "_real_record", lambda r2: (lake, record))
    monkeypatch.setattr(telegram, "send", lambda text: sent.append(text))
    monkeypatch.setattr("nhl_edge.settings.load_env", lambda: None)
    monkeypatch.setattr(
        kt.KalshiTrade,
        "from_env",
        classmethod(lambda cls, env, raw: trade(tmp_path / env, exchange, env=env, now=clock)[0]),
    )
    monkeypatch.setattr("nhl_edge.backtest.reports.version", lambda name, now: rf.CODE)
    monkeypatch.setattr(cli, "_listed_starts", lambda days, mirror: STARTS)

    class Day(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return clock()

    monkeypatch.setattr(cli, "datetime", Day)
    if setting is None:
        monkeypatch.delenv("KALSHI_TRADING", raising=False)
    else:
        monkeypatch.setenv("KALSHI_TRADING", setting)
    return record, exchange, sent, lake


def test_nhl_real_place_with_the_switch_off_places_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, exchange, sent, _ = commands(tmp_path, monkeypatch, None, real_rows())
    result = runner.invoke(app, ["real", "place", "--r2"])
    assert result.exit_code == 0, result.output
    assert "KALSHI_TRADING is off, nothing placed" in result.output
    assert not exchange.requests and not sent


def test_nhl_real_place_live_sends_the_orders_and_prints_counts_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, exchange, sent, _ = commands(tmp_path, monkeypatch, "live", real_rows())
    result = runner.invoke(app, ["real", "place", "--r2"])
    assert result.exit_code == 0, result.output
    assert "kalshi orders live: 3 planned, 3 placed" in result.output
    assert len(exchange.orders) == 3 and len(sent) == 1 and "WSH: buy Yes" in sent[0]
    for secret in (WSH, "order-1", "¢", "$"):
        assert secret not in result.output
    assert record.keys(f"{rl.PREFIX}/orders")


def test_nhl_real_place_live_on_a_practice_day_places_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, exchange, sent, _ = commands(tmp_path, monkeypatch, "live", rf.alert_rows())
    result = runner.invoke(app, ["real", "place", "--r2"])
    assert result.exit_code == 0, result.output
    assert "0 planned, 0 placed" in result.output and not exchange.orders and not sent


def test_nhl_real_place_waits_for_the_midday_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, exchange, _, _ = commands(tmp_path, monkeypatch, "live", real_rows())
    result = runner.invoke(app, ["real", "place", "--r2", "--cron", "45 22 * * *"])
    assert result.exit_code == 0, result.output
    assert "is not today's midday slot: no orders" in result.output and not exchange.requests


def test_a_dry_run_prints_the_orders_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, exchange, sent, _ = commands(tmp_path, monkeypatch, None, rf.alert_rows())
    result = runner.invoke(app, ["real", "place", "--dry-run", "--date", "2026-10-07"])
    assert result.exit_code == 0, result.output
    assert "kalshi orders demo 2026-10-07: 3 would be sent" in result.output
    assert WSH in result.output and not exchange.requests and not sent


def test_nhl_real_orders_with_the_switch_off_cancels_and_rebuilds_the_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, exchange, _, lake = commands(tmp_path, monkeypatch, "live", real_rows())
    assert runner.invoke(app, ["real", "place", "--r2"]).exit_code == 0
    monkeypatch.setenv("KALSHI_TRADING", "off")
    result = runner.invoke(app, ["real", "orders", "--r2"])
    assert result.exit_code == 0, result.output
    assert "kalshi orders live: 0 resting, 0 final" in result.output
    assert "3 cancelled now" in result.output
    assert {o["status"] for o in exchange.orders.values()} == {"canceled"}
    assert lake.replaced == [("real_orders", 3)]


def test_nhl_real_orders_skips_an_environment_without_its_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge.settings import MissingSettingError

    commands(tmp_path, monkeypatch, "demo", real_rows())

    def missing(cls: Any, env: str, raw: Any) -> Any:
        raise MissingSettingError(f"{kt.KEY_VARIABLES[env][0]} is not set.")

    monkeypatch.setattr(kt.KalshiTrade, "from_env", classmethod(missing))
    result = runner.invoke(app, ["real", "orders", "--r2"])
    assert result.exit_code == 0, result.output
    assert "kalshi orders demo: no key set, skipped" in result.output
    assert "kalshi orders live: no key set, skipped" in result.output


def test_nhl_real_place_without_the_schedule_places_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge import cli

    _, exchange, _, _ = commands(tmp_path, monkeypatch, "live", real_rows())
    monkeypatch.setattr(cli, "_listed_starts", lambda days, mirror: None)
    result = runner.invoke(app, ["real", "place", "--r2"])
    assert result.exit_code == 0, result.output
    assert "3 planned, 0 placed" in result.output
    assert "3 the game's start couldn't be checked" in result.output
    assert not exchange.orders


def test_an_order_already_final_is_never_sent_again(tmp_path: Path) -> None:
    # The game moved earlier, the order's deadline passed with nothing sent, then the game moved
    # back: a later run sends nothing for it.
    record, exchange, clock = store(), Exchange(), Clock()
    exchange.status[WSH] = "closed"
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    planned = pm.plans(real_rows(), pm.LIVE)
    pm.place(record, client, planned, clock, rf.CODE, STARTS)
    clock.at = pf.START + timedelta(hours=4)
    pm.reconcile(record, client, clock, STARTS)
    exchange.status.pop(WSH)
    clock.at = PLACED_AT + timedelta(minutes=5)
    report = pm.place(record, client, planned, clock, rf.CODE, STARTS)
    assert report.skipped == {"the order is already final": 3}
    assert WSH not in {o["ticker"] for o in exchange.orders.values()}


def test_an_unsent_order_waits_for_the_schedule_too(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    exchange.status[WSH] = "closed"
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, STARTS)
    clock.at = pf.START + timedelta(hours=4)
    unread = pm.reconcile(record, client, clock, None)
    assert unread.finalized == 0 and unread.waiting == 3 and not rl.entries(record)
