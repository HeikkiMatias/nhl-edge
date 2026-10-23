"""Real bets placed automatically on Kalshi (ADR 0040 and 0045, #281, #321), against the fake
exchange: the switch, the orders' two parts and their checks, never twice, the fills, the entries
and the record, and ADR 0040's one-order records tracked to their end. Every response is synthetic
and no test calls Kalshi."""

import dataclasses
import itertools
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
from nhl_edge.live.exchange import cost, rest

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
ANA = "KXNHLGAME-26OCT07EDMANA-ANA"
# With no asks on the book, each bet's taker part fills nothing and its maker part rests: WSH's
# parts are Kalshi's first two orders, WPG's the next two and ANA's the last two.
WSH_TAKER, WSH_MAKER = "order-1", "order-2"
MAKERS = ("order-2", "order-4", "order-6")
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
    tmp_path: Path,
    rows: pl.DataFrame | None = None,
    setting: str = pm.LIVE,
    exchange: Exchange | None = None,
) -> tuple[lb.R2Store, Exchange, kt.KalshiTrade, Clock, pm.PlaceReport]:
    record, exchange, clock = store(), exchange or Exchange(), Clock()
    exchange.now = clock
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


def ids_of(plan: pm.Plan) -> list[str]:
    return [plan.client_order_id, *(found for _, found in plan.maker_ids)]


def test_a_bets_client_order_ids_are_the_same_on_every_run_and_its_own() -> None:
    live = pm.plans(real_rows(), pm.LIVE)
    again = pm.plans(real_rows(), pm.LIVE)
    demo = pm.plans(real_rows(), pm.DEMO)
    assert [ids_of(p) for p in live] == [ids_of(p) for p in again]
    # a taker part and two maker cents per bet, in each environment
    ids = {found for p in [*live, *demo] for found in ids_of(p)}
    assert len(ids) == 18
    # never ADR 0040's id of the same bet
    row = real_rows().row(0, named=True)
    assert pm.client_order_id(pm.LIVE, row) not in ids


def test_a_skipped_bet_is_never_planned() -> None:
    rows = real_rows().with_columns(
        status=pl.when(pl.col("game_id") == 2026020054)
        .then(pl.lit(rl.SKIP_STARTED))
        .otherwise(pl.col("status")),
        exchange=pl.when(pl.col("game_id") == 2026020054).then(None).otherwise(pl.col("exchange")),
    )
    assert [p.game_id for p in pm.plans(rows, pm.LIVE)] == [2026020053, 2026020055]


# Placing


def test_each_order_is_written_before_it_is_sent_and_its_maker_part_expires_before_its_game(
    tmp_path: Path,
) -> None:
    record, exchange, _, _, report = placed(tmp_path)
    assert report.placed == 3 and report.makers == 3 and not report.skipped
    creates = [json.loads(r.content) for r in exchange.requests if r.method == "POST"]
    first = pm.plans(real_rows(), pm.LIVE)[0]
    assert first.maker is not None and first.maker.limit > first.limit
    assert creates[0] == {
        "ticker": WSH,
        "client_order_id": first.client_order_id,
        "side": "bid",
        "count": f"{first.contracts:.2f}",
        "price": f"{first.limit:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "cancel_order_on_pause": True,
    }
    # the taker part filled nothing, so the maker part has its whole planned cost
    assert creates[1] == {
        "ticker": WSH,
        "client_order_id": dict(first.maker_ids)[49],
        "side": "bid",
        "count": f"{first.maker.contracts:.2f}",
        "price": f"{first.maker.limit:.4f}",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": True,
        "cancel_order_on_pause": True,
        "expiration_time": int(EXPIRES.timestamp()),
    }
    written = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053)))
    taker = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053, pm.TAKER_PLACED)))
    maker = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053, pm.MAKER_PLACED)))
    assert written["execution"] == rl.EXECUTION and written["code_version"] == rf.CODE
    assert written["client_order_id"] == first.client_order_id
    assert written["maker"]["client_order_ids"] == {str(c): i for c, i in first.maker_ids}
    assert taker == {
        "order_id": WSH_TAKER,
        "client_order_id": first.client_order_id,
        "existed": False,
        "placed_utc": PLACED_AT.isoformat(),
    }
    assert maker == {
        "order_id": WSH_MAKER,
        "client_order_id": dict(first.maker_ids)[49],
        "existed": False,
        "placed_utc": PLACED_AT.isoformat(),
        "limit": "0.49",
        "contracts": f"{first.maker.contracts:.2f}",
        "max_cost_usd": f"{first.maker.max_cost_usd:.4f}",
        "crossed": [],
        "taker_contracts": "0.00",
        "taker_cost_usd": "0.000000",
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
    assert seen == [True] * 6


def test_a_rerun_sends_nothing_for_an_order_already_placed(tmp_path: Path) -> None:
    record, exchange, client, clock, _ = placed(tmp_path)
    again = pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, STARTS)
    assert again.already == 3 and again.placed == 0 and again.makers == 0
    assert len(exchange.orders) == 6


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
    # no maker part goes while the taker part's isn't recorded
    assert failed.failed == 1 and failed.placed == 0 and failed.makers == 0
    assert len(exchange.orders) == 1 and tries["n"] == 5
    exchange._create = create  # type: ignore[method-assign]
    again = pm.place(record, client, planned, clock, rf.CODE, STARTS)
    assert again.placed == 1 and again.existed == 1 and again.makers == 1
    assert len(exchange.orders) == 2


def test_an_order_too_close_to_its_expiry_is_skipped(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock(EXPIRES - timedelta(seconds=30))
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    report = pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, STARTS)
    # the two games at 23:30 UTC are skipped, the one at 02:00 goes
    assert report.placed == 1 and report.skipped == {"too close to the start": 2}
    assert [o["ticker"] for o in exchange.orders.values()] == [ANA, ANA]
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
    # the two new ones alone, $3.48, are within $5.80; with the three placed before, each at
    # its larger part's cost, they aren't
    assert sum(p.most_cost() for p in planned) + Decimal("3.48") > Decimal("5.80")
    assert report.skipped == {"over the day cap": 2} and len(exchange.orders) == 6


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
    assert report.skipped == {"over the code's day ceiling": 1} and len(exchange.orders) == 6


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
    """The day's live orders placed, the first one's maker part filled as given, all expired."""
    record, exchange, client, clock, _ = placed(tmp_path)
    for count, price, fee, at in fills or []:
        exchange.fill(WSH_MAKER, count, price, fee, at)
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
        "bet_id": WSH_MAKER,
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
    assert [o["ticker"] for o in exchange.orders.values()] == [ANA, ANA]


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
    assert first.maker is not None
    assert text is not None and text.startswith("NHL Edge · Kalshi real orders · Wed 7 Oct")
    assert (
        f"WSH: buy Yes at most 48¢ at once, {first.contracts} contracts, at most "
        f"${first.max_cost_usd:.2f} ({first.stake:.2f} €): 0.00 filled for $0.00; then a bid at "
        f"49¢ for {first.maker.contracts} contracts until 02:25"
    ) in text
    pm.mark_notified(record, pm.LIVE, told, "placed", clock())
    assert pm.unnotified(record, pm.LIVE, "placed") == []
    assert pm.placed_message(pf.DAY, pm.LIVE, []) is None
    exchange.fill(WSH_MAKER, "1.00", "0.4900", "0.004400", PLACED_AT)
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=4)
    pm.reconcile(record, client, clock, STARTS)
    fills = pm.fills_message(pm.LIVE, pm.unnotified(record, pm.LIVE, "final"))
    assert fills is not None and "2026-10-07 WSH: 1.00 (0.00 at once, 1.00 resting) contracts" in (
        fills
    )
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
    assert len(sent) == 1 and sent[0].count("buy Yes") == 3 and len(exchange.orders) == 6
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
    exchange.now = clock
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
    # Each command's client stores its raw responses apart: the clock stands still here, and
    # two clients' requests would share their names.
    clients = itertools.count()
    monkeypatch.setattr(
        kt.KalshiTrade,
        "from_env",
        classmethod(
            lambda cls, env, raw: trade(
                tmp_path / f"{env}-{next(clients)}", exchange, env=env, now=clock
            )[0]
        ),
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
    assert "3 maker parts resting" in result.output
    assert len(exchange.orders) == 6 and len(sent) == 1 and "WSH: buy Yes" in sent[0]
    for secret in (WSH, "order-1", "order-2", "¢", "$"):
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
    # The odds job wrote the order and sent both parts, then stopped before recording Kalshi's
    # ids: the cancel finds them by their client order ids, records them and cancels the maker
    # part, which rests.
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    planned = pm.plans(real_rows(), pm.LIVE)[:1]
    pm.place(record, client, planned, clock, rf.CODE, STARTS)
    for part in (pm.TAKER_PLACED, pm.MAKER_PLACED):
        record.objects.objects.pop(pm.order_key("live", pf.DAY, 2026020053, part))
    report = pm.reconcile(record, client, clock, STARTS, cancel=True)
    assert report.adopted == 2 and report.cancelled == 1
    assert exchange.orders[WSH_MAKER]["status"] == "canceled"
    taker = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053, pm.TAKER_PLACED)))
    maker = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053, pm.MAKER_PLACED)))
    assert taker["order_id"] == WSH_TAKER and taker["adopted"] is True
    assert maker["order_id"] == WSH_MAKER and maker["adopted"] is True
    assert maker["limit"] == "0.49"


def test_an_order_lost_on_its_way_is_found_before_it_would_be_called_not_sent(
    tmp_path: Path,
) -> None:
    record, exchange, client, clock, _ = placed(tmp_path)
    for part in (pm.TAKER_PLACED, pm.MAKER_PLACED):
        record.objects.objects.pop(pm.order_key("live", pf.DAY, 2026020053, part))
    exchange.fill(WSH_MAKER, "1.00", "0.4900", "0.004400", PLACED_AT)
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=4)
    report = pm.reconcile(record, client, clock, STARTS)
    assert report.adopted == 2
    wsh = next(e for e in rl.entries(record) if e["game_id"] == 2026020053)
    assert wsh["outcome"] == rl.PLACED and wsh["bet_id"] == WSH_MAKER


def test_an_order_lost_on_its_way_is_tracked_and_told_while_it_rests(tmp_path: Path) -> None:
    # Its create reached Kalshi but no answer came back: the next pre-game run finds it, so it
    # is tracked and its message goes before its deadline (Codex on #289).
    record, _, client, clock, _ = placed(tmp_path)
    for part in (pm.TAKER_PLACED, pm.MAKER_PLACED):
        record.objects.objects.pop(pm.order_key("live", pf.DAY, 2026020053, part))
    clock.at = PLACED_AT + timedelta(minutes=10)
    report = pm.reconcile(record, client, clock, STARTS)
    assert report.adopted == 2 and report.resting == 3 and report.finalized == 0
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
    assert report.placed == 1 and report.makers == 1 and report.failed == 0
    for part, order_id in ((pm.TAKER_PLACED, WSH_TAKER), (pm.MAKER_PLACED, WSH_MAKER)):
        stored = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053, part)))
        assert stored["order_id"] == order_id and stored["adopted"] is True


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


# ADR 0045's two parts (#321)


def wsh_plan() -> pm.Plan:
    return pm.plans(real_rows(), pm.LIVE)[0]


def stored(record: lb.R2Store, part: str, game_id: int = 2026020053) -> dict[str, Any]:
    return json.loads(record.get(pm.order_key("live", pf.DAY, game_id, part)))


def test_the_taker_part_takes_the_asks_at_its_limit_and_the_maker_part_rests_for_the_rest(
    tmp_path: Path,
) -> None:
    exchange = Exchange()
    exchange.asks[WSH] = [("0.4700", "1.00"), ("0.4800", "0.50"), ("0.5000", "9.00")]
    record, _, client, clock, report = placed(tmp_path, exchange=exchange)
    assert report.placed == 3 and report.makers == 3
    taker = exchange.orders[WSH_TAKER]
    assert taker["status"] == "canceled" and taker["fill_count_fp"] == "1.50"
    assert all(fill["is_taker"] for fill in exchange.fills[WSH_TAKER])
    # the maker part has what the taker part left of its planned cost, within the stake rule at
    # both parts' price together
    plan = wsh_plan()
    assert plan.maker is not None
    spent = Decimal("0.47") + Decimal("0.0175") + Decimal("0.24") + Decimal("0.0088")
    budget = cost(plan.maker.contracts, plan.maker.limit, KALSHI.exchange, maker=True) - spent  # type: ignore[arg-type]

    def allowed(price: float) -> float:
        return rl.stake_at(plan.stakes, price)

    expected = rest(
        Decimal("0.49"),
        budget,
        (Decimal("1.50"), spent),
        plan.p_side,
        plan.hurdle,
        allowed,
        KALSHI.exchange,  # type: ignore[arg-type]
    )
    maker = stored(record, pm.MAKER_PLACED)
    assert maker["contracts"] == f"{expected:.2f}" and Decimal(maker["contracts"]) > 0
    assert (maker["taker_contracts"], Decimal(maker["taker_cost_usd"])) == ("1.50", spent)
    assert spent + Decimal(maker["max_cost_usd"]) <= plan.maker.max_cost_usd
    # the maker part fills in full; the bet is both parts, within the stake rule
    exchange.fill(WSH_MAKER, maker["contracts"], "0.4900", "0.0030", PLACED_AT + timedelta(hours=1))
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=4)
    pm.reconcile(record, client, clock, STARTS)
    final = stored(record, "final")
    assert final["parts"][pm.TAKER]["contracts"] == "1.50"
    assert final["parts"][pm.MAKER]["contracts"] == maker["contracts"]
    assert Decimal(final["contracts"]) == Decimal("1.50") + Decimal(maker["contracts"])
    assert [fill["is_taker"] for fill in final["fills"]] == [True, True, False]
    assert final["first_fill_utc"] == PLACED_AT.isoformat()
    bets = rl.bets(real_rows(), rl.entries(record))
    wsh = bets.filter(pl.col("game_id") == 2026020053).row(0, named=True)
    assert wsh["outcome"] == rl.PLACED and wsh["within_rule"] is True
    assert wsh["bet_id"] == f"{WSH_TAKER} + {WSH_MAKER}"


def test_a_taker_part_filled_in_full_leaves_the_maker_part_no_stake(tmp_path: Path) -> None:
    exchange = Exchange()
    exchange.asks[WSH] = [("0.4800", "5.00")]
    record, _, client, clock, report = placed(tmp_path, exchange=exchange)
    assert exchange.orders[WSH_TAKER]["status"] == "executed"
    assert report.makers == 2
    assert report.makers_skipped == {"the taker part left no stake for it": 1}
    assert stored(record, pm.MAKER_SKIPPED)["reason"] == "the taker part left no stake for it"
    assert "maker parts not sent: the taker part left no stake for it" in report.line("live")
    # a rerun sends nothing more for it
    again = pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, STARTS)
    assert again.makers == 0 and len(exchange.orders) == 5
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=4)
    pm.reconcile(record, client, clock, STARTS)
    wsh = next(e for e in rl.entries(record) if e["game_id"] == 2026020053)
    assert wsh["outcome"] == rl.PLACED and wsh["bet_id"] == WSH_TAKER
    assert list(stored(record, "final")["parts"]) == [pm.TAKER]


@pytest.mark.parametrize("cross", ["cancel", 400])
def test_a_maker_bid_that_would_cross_the_book_goes_a_cent_lower(
    tmp_path: Path, cross: str | int
) -> None:
    # An ask at 49¢: the taker part's 48¢ can't take it, and a post-only bid at 49¢ would.
    exchange = Exchange()
    exchange.asks[WSH] = [("0.4900", "3.00")]
    exchange.cross = cross
    record, _, _, _, report = placed(tmp_path, exchange=exchange)
    assert report.makers == 3 and report.failed == 0
    maker = stored(record, pm.MAKER_PLACED)
    plan = wsh_plan()
    assert maker["limit"] == "0.48" and maker["crossed"] == [49]
    assert maker["client_order_id"] == dict(plan.maker_ids)[48]
    resting = exchange.orders[maker["order_id"]]
    assert resting["status"] == "resting" and resting["body"]["price"] == "0.4800"
    # at 48¢ the same budget buys at least as many contracts
    assert plan.maker is not None and Decimal(maker["contracts"]) >= plan.maker.contracts
    # a refused bid leaves no order; one Kalshi cancelled at once is on its books, unfilled
    refused = [
        o for o in exchange.orders.values() if o["client_order_id"] == dict(plan.maker_ids)[49]
    ]
    assert len(refused) == (1 if cross == "cancel" else 0)
    assert all(o["status"] == "canceled" and o["fill_count_fp"] == "0.00" for o in refused)


def test_a_maker_bid_kalshi_refuses_at_every_cent_is_skipped_with_its_reason(
    tmp_path: Path,
) -> None:
    exchange = Exchange()
    create = exchange._create

    def refusing(body: dict[str, Any]) -> Any:
        if body["post_only"]:
            return httpx.Response(400, json={"code": "synthetic"})
        return create(body)

    exchange._create = refusing  # type: ignore[method-assign]
    record, _, _, _, report = placed(tmp_path, exchange=exchange)
    reason = "Kalshi refused it at every cent down to the taker limit"
    assert report.makers == 0 and report.makers_skipped == {reason: 3}
    assert stored(record, pm.MAKER_SKIPPED)["reason"] == reason
    assert len(exchange.orders) == 3  # the taker parts alone


def test_a_run_that_stopped_after_the_taker_part_sends_only_the_maker_part_next(
    tmp_path: Path,
) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    planned = pm.plans(real_rows(), pm.LIVE)
    send = pm.send_maker

    def stopping(*args: Any, **kwargs: Any) -> None:
        raise OSError("synthetic: the run stopped")

    pm.send_maker = stopping  # type: ignore[assignment]
    try:
        with pytest.raises(OSError):
            pm.place(record, client, planned, clock, rf.CODE, STARTS)
    finally:
        pm.send_maker = send  # type: ignore[assignment]
    assert len(exchange.orders) == 1  # WSH's taker part
    # its row rewritten in R2 meanwhile: the code's limits refuse the maker part it would size
    first = planned[0]
    assert first.maker is not None
    rich = dataclasses.replace(
        first,
        bankroll=1_000_000.0,
        maker=dataclasses.replace(
            first.maker, contracts=Decimal("9.00"), max_cost_usd=Decimal("4.50")
        ),
    )
    refused = pm.place(record, client, [rich], clock, rf.CODE, STARTS)
    assert refused.makers == 0 and refused.makers_skipped == {"over the code's bet ceiling": 1}
    record.objects.objects.pop(pm.order_key("live", pf.DAY, 2026020053, pm.MAKER_SKIPPED))
    again = pm.place(record, client, planned, clock, rf.CODE, STARTS)
    assert again.already == 1 and again.placed == 2 and again.makers == 3
    bodies = [json.loads(r.content) for r in exchange.requests if r.method == "POST"]
    assert [b["time_in_force"] for b in bodies if b["ticker"] == WSH] == [
        "immediate_or_cancel",
        "good_till_canceled",
    ]


def test_a_maker_part_waits_while_its_taker_part_cant_be_read(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    planned = pm.plans(real_rows(), pm.LIVE)
    create = exchange._create

    def unseen_after(body: dict[str, Any]) -> Any:
        created = create(body)
        if body["client_order_id"] == planned[0].client_order_id:
            exchange.unseen = kt.READ_TRIES  # WSH's taker part never shows in this run
        return created

    exchange._create = unseen_after  # type: ignore[method-assign]
    report = pm.place(record, client, planned, clock, rf.CODE, STARTS)
    assert report.unread == 1 and report.makers == 2 and not report.makers_skipped
    assert "1 maker parts waiting for their taker part" in report.line("live")
    assert not record.keys(pm.order_key("live", pf.DAY, 2026020053, pm.MAKER_SKIPPED))
    text = pm.placed_message(pf.DAY, pm.LIVE, [], unread=report.unread)
    assert text is not None and "Bids for the rest not sent yet: 1" in text
    # the rerun sends it
    again = pm.place(record, client, planned, clock, rf.CODE, STARTS)
    assert again.makers == 1 and stored(record, pm.MAKER_PLACED)["order_id"] == "order-6"


def test_a_maker_part_whose_read_back_fails_is_recorded_and_the_others_go_on(
    tmp_path: Path,
) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    create = exchange._create

    def failing_after(body: dict[str, Any]) -> Any:
        created = create(body)
        if body["post_only"] and body["ticker"] == WSH:
            exchange.fail = [500] * 5  # the read-back fails after its tries
        return created

    exchange._create = failing_after  # type: ignore[method-assign]
    report = pm.place(record, client, pm.plans(real_rows(), pm.LIVE), clock, rf.CODE, STARTS)
    assert report.placed == 3 and report.makers == 3 and report.failed == 0
    assert stored(record, pm.MAKER_PLACED)["order_id"] == WSH_MAKER


def test_a_maker_part_lost_on_its_way_is_found_by_its_cents_id_and_never_a_crossed_one(
    tmp_path: Path,
) -> None:
    exchange = Exchange()
    exchange.asks[WSH] = [("0.4900", "3.00")]
    record, exchange, client, clock, _ = placed(tmp_path, exchange=exchange)
    resting = stored(record, pm.MAKER_PLACED)["order_id"]
    record.objects.objects.pop(pm.order_key("live", pf.DAY, 2026020053, pm.MAKER_PLACED))
    clock.at = PLACED_AT + timedelta(minutes=10)
    report = pm.reconcile(record, client, clock, STARTS)
    assert report.adopted == 1
    adopted = stored(record, pm.MAKER_PLACED)
    assert adopted["order_id"] == resting and adopted["limit"] == "0.48"


def test_a_maker_part_skipped_is_never_looked_for(tmp_path: Path) -> None:
    exchange = Exchange()
    exchange.asks[WSH] = [("0.4800", "5.00")]
    record, exchange, client, clock, _ = placed(tmp_path, exchange=exchange)
    clock.at = PLACED_AT + timedelta(minutes=10)
    before = len(exchange.requests)
    pm.reconcile(record, client, clock, STARTS)
    listings = [r for r in exchange.requests[before:] if r.url.path.endswith("/portfolio/orders")]
    assert not listings


def test_the_maker_parts_cost_counts_against_the_caps_and_its_own_row(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    planned = pm.plans(real_rows(), pm.LIVE)
    first, second = planned[0], planned[1]
    assert first.maker is not None and second.maker is not None
    # 1.5% of 100 € at 1.16 is $1.74: the maker part's planned cost is the bet's most
    over = dataclasses.replace(
        first, maker=dataclasses.replace(first.maker, max_cost_usd=Decimal("1.75"))
    )
    assert over.max_cost_usd < Decimal("1.74") < over.most_cost()
    cheap = dataclasses.replace(
        second, maker=dataclasses.replace(second.maker, contracts=Decimal("50.00"))
    )
    report = pm.place(record, client, [over, cheap, planned[2]], clock, rf.CODE, STARTS)
    assert report.skipped == {"over the bet cap": 1, "it costs more than its row says": 1}
    assert report.placed == 1


def test_an_earlier_runs_order_counts_at_its_larger_parts_recomputed_cost(tmp_path: Path) -> None:
    record, *_ = placed(tmp_path)
    rules = KALSHI.exchange
    assert rules is not None
    total = sum((p.most_cost() for p in pm.plans(real_rows(), pm.LIVE)), Decimal(0))
    assert pm._sent_cost(record, pm.LIVE, pf.DAY, rules) == total
    order = stored(record, "")
    maker = order["maker"]
    assert pm.most_cost(order, rules) == cost(
        Decimal(maker["contracts"]), Decimal(maker["limit"]), rules, maker=True
    )


def test_the_orders_table_shows_both_parts(tmp_path: Path) -> None:
    exchange = Exchange()
    exchange.asks[WSH] = [("0.4700", "1.00")]
    record, exchange, client, clock, _ = placed(tmp_path, exchange=exchange)
    exchange.fill(WSH_MAKER, "0.50", "0.4900", "0.0022", PLACED_AT + timedelta(hours=1))
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=4)
    pm.reconcile(record, client, clock, STARTS)
    row = pm.table(record).filter(pl.col("game_id") == 2026020053).row(0, named=True)
    assert row["execution"] == "adr-0045" and row["order_id"] == WSH_TAKER
    assert (row["maker_limit"], row["maker_order_id"]) == (0.49, WSH_MAKER)
    assert (row["taker_filled"], row["maker_filled"], row["filled"]) == (1.0, 0.5, 1.5)
    assert row["status"] == "canceled"
    assert pm.held_contracts(stored(record, "final")) == Decimal("1.50")


# ADR 0040's one-order records, tracked to their end


def legacy(record: lb.R2Store, client: kt.KalshiTrade, plan: pm.Plan, placed: bool = True) -> str:
    """An order as ADR 0040's runs recorded it: one resting order, with its placed part when
    sent. Returns its order id."""
    order = plan.record(PLACED_AT - timedelta(seconds=1), rf.CODE)
    for name in ("execution", "maker", "p_side", "hurdle", "stakes"):
        order.pop(name)
    row = real_rows().filter(pl.col("game_id") == plan.game_id).row(0, named=True)
    order["client_order_id"] = pm.client_order_id(pm.LIVE, row)
    lb.put_once(record, pm.order_key("live", pf.DAY, plan.game_id), json.dumps(order).encode())
    if not placed:
        return ""
    sent = client.create_order(
        kt.OrderRequest(
            ticker=plan.ticker,
            client_order_id=order["client_order_id"],
            count=plan.contracts,
            price=plan.limit,
            expiration_time=plan.expires_utc,
        )
    )
    placed_record = {
        "order_id": sent.order_id,
        "client_order_id": sent.client_order_id,
        "existed": False,
        "placed_utc": PLACED_AT.isoformat(),
    }
    key = pm.order_key("live", pf.DAY, plan.game_id, pm.PLACED)
    lb.put_once(record, key, json.dumps(placed_record).encode())
    return sent.order_id


def test_an_adr_0040_order_is_tracked_to_its_end_as_one_order(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    plan = wsh_plan()
    order_id = legacy(record, client, plan)
    clock.at = EXPIRES - timedelta(minutes=30)
    assert pm.reconcile(record, client, clock, STARTS).resting == 1
    exchange.fill(order_id, "1.00", "0.4700", "0.017500", PLACED_AT + timedelta(minutes=1))
    clock.at = EXPIRES + pm.GRACE
    report = pm.reconcile(record, client, clock, STARTS)
    assert report.cancelled == 1 and report.finalized == 1 and report.entries == 1
    final = stored(record, "final")
    assert "parts" not in final and final["contracts"] == "1.00"
    assert pm.held_contracts(final) == Decimal("1.00")
    wsh = rl.entries(record)[0]
    assert wsh["outcome"] == rl.PLACED and wsh["bet_id"] == order_id
    row = pm.table(record).row(0, named=True)
    assert row["execution"] == "adr-0040" and row["maker_limit"] is None
    text = pm.fills_message(pm.LIVE, pm.unnotified(record, pm.LIVE, "final"))
    assert text is not None and f"1.00 of {plan.contracts} contracts" in text


def test_a_rerun_never_sends_two_parts_for_an_adr_0040_order(tmp_path: Path) -> None:
    record, exchange, clock = store(), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    planned = pm.plans(real_rows(), pm.LIVE)
    legacy(record, client, planned[0])
    legacy(record, client, planned[1], placed=False)
    report = pm.place(record, client, planned, clock, rf.CODE, STARTS)
    assert report.already == 1 and report.placed == 1 and report.makers == 1
    assert report.skipped == {"the order was planned before ADR 0045": 1}
    tickers = [o["ticker"] for o in exchange.orders.values()]
    assert tickers == [WSH, ANA, ANA]
