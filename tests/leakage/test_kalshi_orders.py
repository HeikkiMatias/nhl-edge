"""Point-in-time rules for the Kalshi orders (ADR 0040, #281), on the synthetic day and the fake
exchange: an order is built only from the day's alert, written before it is sent, and never from a
Kalshi price; it expires before its game, is never sent once that is near, and a fill at or after
the start never becomes a bet (hard rules 1, 2 and 4)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import predict_fixtures as pf
import real_fixtures as rf
from fakes import ConditionalBucket
from kalshi_exchange import Exchange, trade

from nhl_edge.live import bundle as lb
from nhl_edge.live import placement as pm
from nhl_edge.live import real
from nhl_edge.live import real_ledger as rl

KALSHI = real.Venue("kalshi", 0.0, 0.01, real.Exchange("live", 1.16))
REAL_DAY = rl.Mode(rl.REAL, rf.ALERT.date())
STARTS = {game_id: game[2] for game_id, game in pf.GAMES.items()}


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
        placed = json.loads(record.get(pm.order_key("live", pf.DAY, row["game_id"], "placed")))
        assert row["alert_utc"] <= instant(order["written_utc"])
        assert instant(order["written_utc"]) <= instant(placed["placed_utc"])
        # the order is the alert's, number for number
        expected = json.loads(row["exchange"])
        assert (order["limit"], order["contracts"], order["max_cost_usd"]) == (
            expected["limit"],
            expected["contracts"],
            expected["max_cost_usd"],
        )


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
    assert at_low == at_high and len(at_low) == 3


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
    exchange.fill("order-1", "1.00", "0.4700", "0.0175", moved + timedelta(minutes=1))
    clock.at = moved + timedelta(minutes=10)
    pm.reconcile(record, client, clock, starts=starts)
    assert exchange.orders["order-1"]["status"] == "canceled"
    final = json.loads(record.get(pm.order_key("live", pf.DAY, 2026020053, "final")))
    assert final["after_start"] is True and final["contracts"] == "0.00"
    wsh = next(e for e in rl.entries(record) if e["game_id"] == 2026020053)
    assert wsh["outcome"] == rl.NOT_PLACED


def test_no_model_code_reads_the_real_record_or_the_orders() -> None:
    # The real record and the orders are the owner's own (ADR 0039, 0040), never a model input.
    src = Path(__file__).parents[2] / "src" / "nhl_edge"
    names = ("real_orders", "real_bets", "real_settlements", "real_alerts", "placement", "exchange")
    for package in ("features", "game", "ratings", "lineup", "market", "betting", "backtest"):
        for path in (src / package).rglob("*.py"):
            text = path.read_text()
            imports = [line for line in text.splitlines() if line.startswith(("import", "from"))]
            assert not any(name in text for name in names[:4]), path
            assert not any(
                f"live.{module}" in line or f"live import {module}" in line
                for line in imports
                for module in ("placement", "exchange", "real_ledger", "kalshi_trade")
            ), path


def test_a_fill_at_or_after_the_start_never_becomes_a_bet(tmp_path: Path) -> None:
    record, exchange, clock = lb.R2Store(ConditionalBucket(), "bucket"), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    pm.place(record, client, pm.plans(rows(), pm.LIVE), clock, rf.CODE, STARTS)
    exchange.fill("order-1", "1.00", "0.4700", "0.0175", pf.START)
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=5)
    pm.reconcile(record, client, clock)
    entries = rl.entries(record)
    wsh = next(e for e in entries if e["game_id"] == 2026020053)
    assert wsh["outcome"] == rl.NOT_PLACED  # its only fill came at the start
    placed = [e for e in entries if e["outcome"] == rl.PLACED]
    starts = {g: pf.GAMES[g][2] for g in pf.GAMES}
    assert all(instant(e["placed_utc"]) < starts[e["game_id"]] for e in placed)
