"""#321's point-in-time rules for re-valuing a resting maker part (ADR 0045 item 2): it reads only
quotes, tops of book and Daily Faceoff pages observed before it; Kalshi's prices never reach p';
a bid is never raised; the code's limits still hold; and a triggered snapshot never enters the
odds the closing proxy is read from (hard rules 1, 3 and 4)."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import predict_fixtures as pf
import pytest
import revalue_fixtures as vf

from nhl_edge.ingest import odds
from nhl_edge.live import placement as pm
from nhl_edge.live import real
from nhl_edge.live import revalue as rv
from nhl_edge.live.exchange import cost

AFTER = vf.SLOT + timedelta(minutes=1)


@pytest.fixture(autouse=True)
def kalshi_venue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(real, "VENUES", {"kalshi": vf.KALSHI})
    monkeypatch.setattr(real, "VENUE", "kalshi")


def run(record: Any, raw: Any, client: Any, clock: vf.Clock, **kwargs: Any) -> rv.RevalueReport:
    parts, _ = rv.resting(record, pm.LIVE, rv.ledger_of(record), clock())
    rules = pm.code_exchange(pm.LIVE)
    assert rules is not None
    return rv.revalue(record, raw, client, pf.live(), rules, parts, clock, **kwargs)


def test_a_quote_taken_after_the_valuation_is_never_read(tmp_path: Path) -> None:
    record, raw, exchange, client, clock = vf.placed(tmp_path)
    taken = AFTER + timedelta(minutes=1)
    body = vf.odds_body({2026020053: (2.60, 1.52)}, taken)
    key = vf.store_slot(raw, taken, body)
    clock.at = AFTER
    report = run(record, raw, client, clock, slot=rv.Snapshot(body, taken, key))
    assert report.valued == 0 and not exchange.amends
    quote = rv.Quote(taken, taken, 2.6, 1.52, key, pf.GAMES[2026020053][2])
    assert not quote.fresh(AFTER) and quote.fresh(taken)


def test_a_quote_for_a_game_under_way_is_never_fresh(tmp_path: Path) -> None:
    # The Odds API's start before the schedule's: its price at AFTER is in-play, not pre-game
    record, raw, exchange, client, clock = vf.placed(tmp_path)
    body = vf.odds_body({2026020053: (2.60, 1.52)}, vf.SLOT, commence=AFTER - timedelta(seconds=1))
    key = vf.store_slot(raw, vf.SLOT, body)
    clock.at = AFTER
    report = run(record, raw, client, clock, slot=rv.Snapshot(body, vf.SLOT, key))
    assert report.valued == 0 and not exchange.amends
    assert [r["action"] for r in vf.records(record)] == [rv.STALE]


def test_a_top_of_book_stored_after_the_check_moves_nothing(tmp_path: Path) -> None:
    record, raw, _, client, clock = vf.placed(tmp_path)
    vf.store_tops(raw, vf.PLACED_AT - timedelta(minutes=1), {vf.WSH: ("0.4500", "0.5200")})
    clock.at = AFTER
    vf.store_tops(raw, AFTER + timedelta(minutes=1), {vf.WSH: ("0.4500", "0.6000")})
    calls: list[httpx.Request] = []
    assert run(record, raw, client, clock, odds=vf.odds_api(b"[]", clock, calls)).moved == 0
    assert not calls


def test_the_decisions_starters_come_only_from_pages_before_it(tmp_path: Path) -> None:
    record, raw, _, client, clock = vf.placed(tmp_path)
    vf.store_page(raw, pf.DECISION + timedelta(seconds=1), {"WSH": 1, "PIT": 2})
    vf.store_page(raw, AFTER - timedelta(minutes=1), {"WSH": 7, "PIT": 2})
    clock.at = AFTER
    parts, _ = rv.resting(record, pm.LIVE, rv.ledger_of(record), AFTER)
    wsh = next(p for p in parts if p.game_id == 2026020053)
    assert rv.starters(raw, wsh, wsh.prediction_utc) is None
    assert run(record, raw, client, clock).starters == 0


def test_a_page_fetched_after_the_check_changes_no_starter(tmp_path: Path) -> None:
    record, raw, exchange, client, clock = vf.placed(tmp_path)
    vf.store_page(raw, pf.DECISION - timedelta(minutes=5), {"WSH": 1, "PIT": 2})
    vf.store_page(raw, AFTER + timedelta(minutes=1), {"WSH": 7, "PIT": 2})
    clock.at = AFTER
    parts, _ = rv.resting(record, pm.LIVE, rv.ledger_of(record), AFTER)
    wsh = next(p for p in parts if p.game_id == 2026020053)
    assert rv.starters(raw, wsh, AFTER) == rv.starters(raw, wsh, wsh.prediction_utc)
    assert run(record, raw, client, clock).starters == 0
    assert exchange.orders[vf.WSH_MAKER]["status"] == "resting"


def test_no_kalshi_price_reaches_p_prime(tmp_path: Path) -> None:
    # The market's prices served far apart, and tops of book far apart: p' and the action are
    # the same, since only Pinnacle's pair, the decision's B3 and u and the hurdle value a part.
    found = []
    for name, (bid, ask) in (("low", ("0.0500", "0.0600")), ("high", ("0.9400", "0.9500"))):
        record, raw, exchange, client, clock = vf.placed(tmp_path / name)
        exchange.market_fields = {"yes_bid_dollars": bid, "yes_ask_dollars": ask}
        vf.store_tops(raw, AFTER - timedelta(seconds=30), {vf.WSH: (bid, ask)})
        body = vf.odds_body({2026020053: (2.20, 1.72)}, vf.SLOT - timedelta(seconds=20))
        key = vf.store_slot(raw, vf.SLOT, body)
        clock.at = AFTER
        run(record, raw, client, clock, slot=rv.Snapshot(body, vf.SLOT, key))
        valued = vf.records(record)[-1]
        found.append((valued["p_side"], valued["action"], valued["limit_after"]))
    assert found[0] == found[1]


@pytest.mark.parametrize(
    "prices", [(1.50, 2.80), (1.95, 1.93), (2.10, 1.80), (2.20, 1.72), (2.60, 1.52), (4.0, 1.28)]
)
def test_a_bid_is_never_raised_and_never_costs_more(
    tmp_path: Path, prices: tuple[float, float]
) -> None:
    record, raw, exchange, client, clock = vf.placed(tmp_path)
    body = vf.odds_body({2026020053: prices}, vf.SLOT - timedelta(seconds=20))
    key = vf.store_slot(raw, vf.SLOT, body)
    clock.at = AFTER
    run(record, raw, client, clock, slot=rv.Snapshot(body, vf.SLOT, key))
    valued = vf.records(record)[-1]
    before = Decimal(valued["limit_before"])
    if valued["limit_after"] is not None:
        assert Decimal(valued["limit_after"]) <= before
    rules = pm.code_exchange(pm.LIVE)
    assert rules is not None
    for order_id, sent in exchange.amends:
        order = exchange.orders[order_id]
        count = Decimal(sent["count"])
        # the count stays the order's (filled plus resting), so its most cost only falls
        assert count == Decimal(order["fill_count_fp"]) + Decimal(order["remaining_count_fp"])
        assert cost(count, Decimal(sent["price"]), rules, maker=True) <= cost(
            count, before, rules, maker=True
        )
        # and the lower bid still clears the hurdle at p'
        total = cost(count, Decimal(sent["price"]), rules, maker=True)
        assert valued["p_side"] * float(count / total) - 1 >= 0.025


def test_a_triggered_snapshot_never_enters_the_odds_the_closing_proxy_reads(
    tmp_path: Path,
) -> None:
    record, raw, _, client, clock = vf.placed(tmp_path)
    vf.store_tops(raw, vf.PLACED_AT - timedelta(minutes=1), {vf.WSH: ("0.4500", "0.5200")})
    clock.at = AFTER
    vf.store_tops(raw, AFTER - timedelta(minutes=1), {vf.WSH: ("0.4500", "0.5600")})
    calls: list[httpx.Request] = []
    body = vf.odds_body({2026020053: (2.20, 1.72)}, AFTER - timedelta(seconds=30))
    assert run(record, raw, client, clock, odds=vf.odds_api(body, clock, calls)).calls == 1
    # odds_snapshots and the closing proxy are rebuilt from raw/odds/ only
    assert odds.SOURCE == "odds" != rv.SOURCE
    assert not list(raw.base_dir.glob("odds/**/*.json.gz"))
    assert list(raw.base_dir.glob(f"{rv.SOURCE}/**/*.json.gz"))
