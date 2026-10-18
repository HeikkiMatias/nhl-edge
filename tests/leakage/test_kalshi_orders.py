"""Point-in-time rules for the Kalshi orders (ADR 0040, #281), on the synthetic day and the fake
exchange: an order is built only from the day's alert, written before it is sent, and never from a
Kalshi price; it expires before its game, is never sent once that is near, and a fill at or after
the start never becomes a bet (hard rules 1, 2 and 4)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
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
    pm.place(record, client, pm.plans(rows(), pm.LIVE), clock, rf.CODE)
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
    def priced(ask: str) -> Exchange:
        exchange = Exchange()
        route = exchange.__call__

        def with_prices(request: httpx.Request) -> httpx.Response:
            response = route(request)
            if request.url.path.startswith("/trade-api/v2/markets/"):
                market = response.json()["market"] | {
                    "yes_ask_dollars": ask,
                    "yes_bid_dollars": ask,
                }
                return httpx.Response(200, json={"market": market})
            return response

        exchange.__call__ = with_prices  # type: ignore[method-assign]
        return exchange

    low, high = priced("0.0500"), priced("0.9500")
    _, at_low = sent(tmp_path / "low", low)
    _, at_high = sent(tmp_path / "high", high)
    assert at_low == at_high and len(at_low) == 3


def test_every_order_expires_before_its_game_and_none_goes_once_it_is_near(
    tmp_path: Path,
) -> None:
    for plan in pm.plans(rows(), pm.LIVE):
        assert plan.expires_utc < plan.start_utc
    record, exchange, clock = lb.R2Store(ConditionalBucket(), "bucket"), Exchange(), Clock()
    clock.at = pf.START - real.Exchange("live", 1.0).expire_before - pm.LEAD
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    report = pm.place(record, client, pm.plans(rows(), pm.LIVE), clock, rf.CODE)
    assert report.skipped == {"too close to the start": 2}
    assert all(o["ticker"].endswith("-ANA") for o in exchange.orders.values())


def test_a_fill_at_or_after_the_start_never_becomes_a_bet(tmp_path: Path) -> None:
    record, exchange, clock = lb.R2Store(ConditionalBucket(), "bucket"), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    pm.place(record, client, pm.plans(rows(), pm.LIVE), clock, rf.CODE)
    exchange.fill("order-1", "1.00", "0.4700", "0.0175", pf.START)
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=5)
    pm.reconcile(record, client, clock)
    entries = rl.entries(record)
    assert 2026020053 not in {e["game_id"] for e in entries}
    placed = [e for e in entries if e["outcome"] == rl.PLACED]
    starts = {g: pf.GAMES[g][2] for g in pf.GAMES}
    assert all(instant(e["placed_utc"]) < starts[e["game_id"]] for e in placed)
