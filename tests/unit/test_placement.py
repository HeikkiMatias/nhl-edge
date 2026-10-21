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
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import RealOrders
from nhl_edge.live import bundle as lb
from nhl_edge.live import kalshi_trade as kt
from nhl_edge.live import placement as pm
from nhl_edge.live import real
from nhl_edge.live import real_ledger as rl
from nhl_edge.live.exchange import cost

KALSHI = real.Venue("kalshi", 0.0, 0.01, real.Exchange("live", 1.16))


@pytest.fixture(autouse=True)
def kalshi_venue(monkeypatch: pytest.MonkeyPatch) -> None:
    """The code names Kalshi's live exchange, as the venue commit will (#294)."""
    monkeypatch.setattr(real, "VENUES", {"kalshi": KALSHI})
    monkeypatch.setattr(real, "VENUE", "kalshi")


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
    failed = pm.place(record, client, planned, clock, rf.CODE, STARTS)
    assert failed.failed == 1 and failed.placed == 0
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


def tampered(plan: pm.Plan, contracts: str, **changes: Any) -> pm.Plan:
    """A plan as a rewritten alert row in R2 could make it: contracts at its limit, at an honest
    cost, with any other field changed."""
    count = Decimal(contracts)
    honest = cost(count, plan.limit, KALSHI.exchange)  # type: ignore[arg-type]
    return dataclasses.replace(plan, contracts=count, max_cost_usd=honest, **changes)


def test_a_row_from_r2_never_sizes_an_order_past_the_codes_ceiling(tmp_path: Path) -> None:
    # A rewritten row claims a bankroll of a million euros: its own cap passes, the code's
    # ceiling (1.5% of 200 € at 1.16, $3.48) doesn't (#294).
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    planned = pm.plans(real_rows(), pm.LIVE)
    rich = tampered(planned[0], "10.00", bankroll=1_000_000.0)
    assert rich.max_cost_usd > Decimal("3.48")
    report = pm.place(record, client, [rich, *planned[1:]], clock, rf.CODE, STARTS)
    assert report.skipped == {"over the code's bet ceiling": 1} and report.placed == 2


def costing(plan: pm.Plan, at_most: str, **changes: Any) -> pm.Plan:
    """The plan with the most contracts, in 0.01s, whose honest cost is at most at_most."""
    rules, step, ceiling = KALSHI.exchange, Decimal("0.01"), Decimal(at_most)
    count = step
    while cost(count + step, plan.limit, rules) <= ceiling:  # type: ignore[arg-type]
        count += step
    return tampered(plan, str(count), **changes)


def test_the_days_orders_stay_within_the_codes_day_ceiling(tmp_path: Path) -> None:
    # Four orders under the bet ceiling, about $3.40 each, go over the day's $11.60 (5% of
    # 200 € at 1.16), whatever bankroll their rows claim.
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    planned = pm.plans(real_rows(), pm.LIVE)
    extra = dataclasses.replace(planned[0], game_id=2026020099, client_order_id="other")
    rich = [costing(p, "3.40", bankroll=1_000_000.0) for p in [*planned, extra]]
    assert all(Decimal("3.30") < p.max_cost_usd <= Decimal("3.40") for p in rich)
    report = pm.place(record, client, rich, clock, rf.CODE, STARTS)
    assert report.skipped == {"over the code's day ceiling": 4} and not exchange.orders


def test_the_day_ceiling_counts_earlier_orders_from_their_records_not_the_rows(
    tmp_path: Path,
) -> None:
    # Three orders of about $3.40 went out; then the alert was rewritten to make them look nearly
    # free and to add a fourth. Their records, not the rows, count (Codex on #295).
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    planned = pm.plans(real_rows(), pm.LIVE)
    big = [costing(p, "3.40", bankroll=1_000_000.0) for p in planned]
    assert pm.place(record, client, big, clock, rf.CODE, STARTS).placed == 3
    shrunk = [dataclasses.replace(p, max_cost_usd=Decimal("0.01")) for p in big]
    late = costing(
        dataclasses.replace(planned[0], game_id=2026020099, client_order_id="late"),
        "3.40",
        bankroll=1_000_000.0,
    )
    # by the rows, $0.03 sent and $3.40 new; by the records, over $13 against $11.60
    report = pm.place(record, client, [*shrunk, late], clock, rf.CODE, STARTS)
    assert report.skipped == {"over the code's day ceiling": 1} and len(exchange.orders) == 3


def test_a_row_that_understates_its_cost_or_changes_the_rate_is_refused(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    planned = pm.plans(real_rows(), pm.LIVE)
    cheap = dataclasses.replace(planned[0], contracts=Decimal("50.00"))  # its cost unchanged
    rated = dataclasses.replace(planned[1], usd_per_eur=10.0)
    report = pm.place(record, client, [cheap, rated, planned[2]], clock, rf.CODE, STARTS)
    assert report.skipped == {
        "it costs more than its row says": 1,
        "its rate isn't the code's": 1,
    }
    assert report.placed == 1


def test_no_live_order_goes_before_the_code_names_the_venue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A row in R2 with a live order is not enough: the venue commit is (#294).
    monkeypatch.setattr(real, "VENUE", None)
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    report = pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, STARTS)
    assert report.skipped == {"the code names no exchange for its environment": 3}
    assert not exchange.orders
    assert pm.code_exchange(pm.DEMO) is real.DEMO_EXCHANGE


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
        pm.fills_message(pm.LIVE, pm.unnotified(record, pm.LIVE, "final"))
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


def test_the_messages_give_the_orders_and_the_fills_each_once(tmp_path: Path) -> None:
    record, exchange, client, clock, report = placed(tmp_path)
    told = pm.unnotified(record, pm.LIVE, "placed")
    text = pm.placed_message(pf.DAY, pm.LIVE, told, report.skipped)
    first = pm.plans(real_rows(), pm.LIVE)[0]
    assert text is not None and text.startswith("NHL Edge · Kalshi real orders · Wed 7 Oct")
    assert f"WSH: buy Yes at most {round(first.limit * 100)}¢, {first.contracts} contracts" in text
    pm.mark_notified(record, pm.LIVE, told, "placed", clock())
    assert pm.unnotified(record, pm.LIVE, "placed") == []
    assert pm.placed_message(pf.DAY, pm.LIVE, []) is None
    exchange.fill("order-1", "1.00", "0.4700", "0.017500", PLACED_AT)
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=4)
    pm.reconcile(record, client, clock, STARTS)
    fills = pm.fills_message(pm.LIVE, pm.unnotified(record, pm.LIVE, "final"))
    assert fills is not None and "2026-10-07 WSH: 1.00 of" in fills
    assert "2026-10-07 WPG: nothing filled" in fills


def test_a_message_telegram_refused_goes_with_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge.live import telegram

    _, exchange, sent, _ = commands(tmp_path, monkeypatch, "live", real_rows())

    def refused(text: str) -> None:
        raise telegram.TelegramError("synthetic")

    monkeypatch.setattr(telegram, "send", refused)
    assert runner.invoke(app, ["real", "place", "--r2"]).exit_code == 1
    monkeypatch.setattr(telegram, "send", lambda text: sent.append(text))
    result = runner.invoke(app, ["real", "place", "--r2"])
    assert result.exit_code == 0, result.output
    assert len(sent) == 1 and sent[0].count("buy Yes") == 3 and len(exchange.orders) == 3
    assert runner.invoke(app, ["real", "place", "--r2"]).exit_code == 0
    assert len(sent) == 1  # told once


def test_a_demo_message_says_so(tmp_path: Path) -> None:
    record, *_ = placed(tmp_path, rf.alert_rows(), pm.DEMO)
    text = pm.placed_message(pf.DAY, pm.DEMO, pm.unnotified(record, pm.DEMO, "placed"))
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
    # No R2 here, as on CI: the trade's raw responses go to a local store.
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        RawStore,
        "from_env",
        classmethod(lambda cls, *args, **kwargs: RawStore(tmp_path / "cli-raw")),
    )
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


def unreadable(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    """The environment's key can't be read (#300), and the other environment's still works."""
    working = kt.KalshiTrade.from_env

    def from_env(cls: Any, name: str, raw: Any) -> Any:
        if name == env:
            kt.load_key("pasted-wrong", kt.KEY_VARIABLES[name][1])
        return working(name, raw)

    monkeypatch.setattr(kt.KalshiTrade, "from_env", classmethod(from_env))


def test_nhl_real_orders_warns_of_a_key_nothing_needs_and_tracks_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands(tmp_path, monkeypatch, "live", real_rows())
    assert runner.invoke(app, ["real", "place", "--r2"]).exit_code == 0
    unreadable(monkeypatch, pm.DEMO)
    result = runner.invoke(app, ["real", "orders", "--r2"])
    assert result.exit_code == 0, result.output
    assert "::warning::kalshi orders demo: KALSHI_DEMO_PRIVATE_KEY is no unencrypted" in (
        result.output
    )
    assert "kalshi orders live: 3 resting" in result.output
    assert "pasted-wrong" not in result.output


def test_nhl_real_orders_tells_a_skipped_environments_queued_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Orders final, their messages never sent, and then the key can't be read (Codex on #301).
    from nhl_edge import cli

    record, exchange, client, clock, _ = placed(tmp_path)
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=4)
    pm.reconcile(record, client, clock, STARTS)
    assert not pm.pending_dates(record, pm.LIVE) and pm.unnotified(record, pm.LIVE, "final")
    _, _, sent, lake = commands(tmp_path / "cli", monkeypatch, "demo", real_rows())
    monkeypatch.setattr(cli, "_real_record", lambda r2: (lake, record))
    unreadable(monkeypatch, pm.LIVE)
    result = runner.invoke(app, ["real", "orders", "--r2"])
    assert result.exit_code == 0, result.output
    assert "::warning::kalshi orders live:" in result.output
    assert len(sent) == 2 and "fills" in sent[1]
    assert not pm.unnotified(record, pm.LIVE, "placed")
    assert not pm.unnotified(record, pm.LIVE, "final")


@pytest.mark.parametrize("case", ["switch", "order", "cancel"])
def test_nhl_real_orders_fails_on_a_key_the_switch_an_order_or_a_cancel_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    # The switch naming the environment, an order of it waiting while the switch is off, or the
    # kill switch's --cancel with nothing recorded (Codex on #301).
    commands(tmp_path, monkeypatch, "demo", real_rows())
    if case == "order":
        assert runner.invoke(app, ["real", "place", "--r2"]).exit_code == 0
    if case != "switch":
        monkeypatch.setenv("KALSHI_TRADING", "off")
    unreadable(monkeypatch, pm.DEMO)
    flags = ["--cancel"] if case == "cancel" else []
    result = runner.invoke(app, ["real", "orders", "--r2", *flags])
    assert result.exit_code == 1, result.output
    assert "kalshi orders demo: failed (KALSHI_DEMO_PRIVATE_KEY is no unencrypted" in (
        result.output
    )
    assert "::warning::" not in result.output


def test_nhl_real_place_without_the_schedule_places_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge import cli

    _, exchange, sent, _ = commands(tmp_path, monkeypatch, "live", real_rows())
    monkeypatch.setattr(cli, "_listed_starts", lambda days, mirror: None)
    result = runner.invoke(app, ["real", "place", "--r2"])
    assert result.exit_code == 0, result.output
    assert "3 planned, 0 placed" in result.output
    assert "3 the game's start couldn't be checked" in result.output
    assert not exchange.orders
    # The alert said the bets go automatically, so the owner hears that none went.
    assert len(sent) == 1 and "Not placed: 3 the game's start couldn't be checked" in sent[0]


def test_the_schedule_is_read_from_the_day_before_each_game_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A game moved earlier, into the day before its date, is still listed (Codex on #289).
    from datetime import date

    from nhl_edge import cli
    from nhl_edge.ingest.nhl_api import NhlApi, ScheduledGame

    asked: list[date] = []
    moved = ScheduledGame(2026020053, 2, pf.START - timedelta(days=1), "WSH", "PIT")

    def schedule(api: Any, day: date) -> list[ScheduledGame]:
        asked.append(day)
        return [moved]

    monkeypatch.setattr(NhlApi, "schedule", schedule)
    monkeypatch.setattr(
        RawStore, "from_env", classmethod(lambda cls, *args, **kwargs: RawStore(tmp_path))
    )
    starts = cli._listed_starts({pf.DAY}, False)
    assert asked == [pf.DAY - timedelta(days=1)]
    assert starts == {2026020053: pf.START - timedelta(days=1)}


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
    # the two sent before count as placed; the one never sent is final and stays unsent
    assert report.skipped == {"the order is already final": 1} and report.already == 2
    assert WSH not in {o["ticker"] for o in exchange.orders.values()}


def test_an_unsent_order_waits_for_the_schedule_too(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    exchange.status[WSH] = "closed"
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, STARTS)
    clock.at = pf.START + timedelta(hours=4)
    unread = pm.reconcile(record, client, clock, None)
    assert unread.finalized == 0 and unread.waiting == 3 and not rl.entries(record)


# Codex on #289


def test_an_entry_a_run_missed_after_its_final_record_is_written_by_the_next(
    tmp_path: Path,
) -> None:
    record, _, client, clock = finish(tmp_path, [("1.00", "0.4700", "0.017500", PLACED_AT)])
    entries = rl.store_entries
    calls = {"n": 0}

    def failing(store: Any, found: Any) -> int:
        calls["n"] += 1
        raise OSError("synthetic: the run stopped")

    rl.store_entries = failing  # type: ignore[assignment]
    try:
        with pytest.raises(OSError):
            pm.reconcile(record, client, clock, STARTS)
    finally:
        rl.store_entries = entries  # type: ignore[assignment]
    assert record.keys(f"{rl.PREFIX}/orders") and not rl.entries(record)
    later = pm.reconcile(record, client, clock, STARTS)
    assert later.entries == 3
    assert next(e for e in rl.entries(record) if e["game_id"] == 2026020053)["outcome"] == rl.PLACED


def test_a_fill_under_half_a_cent_still_stakes_a_cent(tmp_path: Path) -> None:
    record, _, client, clock = finish(tmp_path, [("0.01", "0.4700", "0.000100", PLACED_AT)])
    pm.reconcile(record, client, clock, STARTS)
    wsh = next(e for e in rl.entries(record) if e["game_id"] == 2026020053)
    assert wsh["outcome"] == rl.PLACED and wsh["stake"] == 0.01
    rl.bets(real_rows(), rl.entries(record))  # the record takes it


def test_the_kill_switch_finds_an_order_its_run_never_recorded(tmp_path: Path) -> None:
    # The odds job wrote the order and sent it, then stopped before recording Kalshi's id: the
    # cancel finds it by its client order id, records it and cancels it.
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    planned = pm.plans(real_rows(), pm.LIVE)[:1]
    pm.place(record, client, planned, clock, rf.CODE, STARTS)
    record.objects.objects.pop(pm.order_key("live", pf.DAY, 2026020053, "placed"))
    report = pm.reconcile(record, client, clock, STARTS, cancel=True)
    assert report.adopted == 1 and report.cancelled == 1
    assert exchange.orders["order-1"]["status"] == "canceled"
    adopted = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053, "placed")))
    assert adopted["order_id"] == "order-1" and adopted["adopted"] is True


def test_an_order_lost_on_its_way_is_found_before_it_would_be_called_not_sent(
    tmp_path: Path,
) -> None:
    record, exchange, client, clock, _ = placed(tmp_path)
    record.objects.objects.pop(pm.order_key("live", pf.DAY, 2026020053, "placed"))
    exchange.fill("order-1", "1.00", "0.4700", "0.017500", PLACED_AT)
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=4)
    report = pm.reconcile(record, client, clock, STARTS)
    assert report.adopted == 1
    wsh = next(e for e in rl.entries(record) if e["game_id"] == 2026020053)
    assert wsh["outcome"] == rl.PLACED and wsh["bet_id"] == "order-1"


def test_an_order_lost_on_its_way_is_tracked_and_told_while_it_rests(tmp_path: Path) -> None:
    # Its create reached Kalshi but no answer came back: the next pre-game run finds it, so it
    # is tracked and its message goes before its deadline (Codex on #289).
    record, _, client, clock, _ = placed(tmp_path)
    record.objects.objects.pop(pm.order_key("live", pf.DAY, 2026020053, "placed"))
    clock.at = PLACED_AT + timedelta(minutes=10)
    report = pm.reconcile(record, client, clock, STARTS)
    assert report.adopted == 1 and report.resting == 3 and report.finalized == 0
    told = pm.unnotified(record, pm.LIVE, "placed")
    assert 2026020053 in {r["game_id"] for r in told}
    assert pm.table(record).filter(pl.col("game_id") == 2026020053)["status"].item() == "placed"


def test_a_failed_look_for_an_unrecorded_order_waits_without_stopping_the_others(
    tmp_path: Path,
) -> None:
    # WSH was never sent (its market was closed); looking for it fails before its deadline.
    record, exchange, clock = store(), Exchange(), Clock()
    exchange.status[WSH] = "closed"
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, STARTS)
    clock.at = PLACED_AT + timedelta(minutes=10)
    exchange.fail = [400]
    report = pm.reconcile(record, client, clock, STARTS)
    assert report.waiting == 1 and report.resting == 2
    # Once a cancel needs it, the failure stops the run, so nothing is left unlooked for.
    exchange.fail = [400]
    with pytest.raises(kt.KalshiTradeError):
        pm.reconcile(record, client, clock, STARTS, cancel=True)


def test_an_order_adopted_while_its_run_records_it_counts_as_placed(tmp_path: Path) -> None:
    # A reconcile running meanwhile records the order first: the placing run moves on.
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    planned = pm.plans(real_rows(), pm.LIVE)[:1]
    create = client.create_order

    def create_then_adopt(request: Any) -> Any:
        sent = create(request)
        pm.reconcile(record, client, clock, STARTS)
        return sent

    client.create_order = create_then_adopt  # type: ignore[method-assign]
    report = pm.place(record, client, planned, clock, rf.CODE, STARTS)
    assert report.placed == 1 and report.failed == 0
    stored = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053, "placed")))
    assert stored["order_id"] == "order-1" and stored["adopted"] is True


def test_a_skip_a_later_run_overcame_isnt_the_sent_orders_reason(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    planned = pm.plans(real_rows(), pm.LIVE)
    first = pm.place(record, client, planned, clock, rf.CODE, {})
    assert first.skipped == {"the game's start couldn't be checked": 3}
    assert pm.place(record, client, planned, clock, rf.CODE, STARTS).placed == 3
    orders = pm.table(record)
    assert orders["status"].to_list() == ["placed"] * 3
    assert orders["reason"].to_list() == [None] * 3


def test_the_owner_hears_of_a_day_whose_orders_all_stayed_unsent() -> None:
    text = pm.placed_message(pf.DAY, pm.LIVE, [], {"the balance doesn't cover the orders": 2}, 1)
    assert text is not None and text.startswith("NHL Edge · Kalshi real orders · Wed 7 Oct")
    assert "Not placed: 2 the balance doesn't cover the orders" in text
    assert "Failed to send: 1" in text


def test_a_market_that_cant_be_read_skips_only_its_own_order(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    route = exchange.__class__.__call__

    class Missing(Exchange):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(f"/markets/{WSH}"):
                self.requests.append(request)
                return httpx.Response(404, json={"code": "not_found"})
            return route(self, request)

    missing = Missing()
    client, _ = trade(tmp_path / "missing", missing, env=pm.LIVE, now=clock)
    report = pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, STARTS)
    assert report.skipped == {"the market couldn't be read": 1} and report.placed == 2


def test_the_alert_says_when_kalshi_trading_isnt_live() -> None:
    rows = real_rows()
    off = rl.message(rf.ALERT.date(), REAL_DAY, rows, 100.0, None, placing=False)
    on = rl.message(rf.ALERT.date(), REAL_DAY, rows, 100.0, None)
    assert "KALSHI_TRADING isn't live, so no order goes to Kalshi today" in off
    assert "The orders go to Kalshi automatically" not in off
    assert "The orders go to Kalshi automatically" in on
