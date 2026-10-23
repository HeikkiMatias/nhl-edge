"""Re-valuing a resting maker part (ADR 0045 item 2, #321), against the fake exchange: the slot's
snapshot, Kalshi's move and a starter change; lowered, cancelled or resting, never raised; the
Odds API cap; the record, the messages and the reconcile after. Every response is synthetic."""

import itertools
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import predict_fixtures as pf
import pytest
import revalue_fixtures as vf
from kalshi_exchange import Exchange
from typer.testing import CliRunner

from nhl_edge.cli import app
from nhl_edge.live import bundle as lb
from nhl_edge.live import placement as pm
from nhl_edge.live import real
from nhl_edge.live import revalue as rv
from nhl_edge.live.exchange import cost
from nhl_edge.live.predict import devig


@pytest.fixture(autouse=True)
def kalshi_venue(monkeypatch: pytest.MonkeyPatch) -> None:
    """The code names Kalshi's live exchange, as the venue commit will (#294)."""
    monkeypatch.setattr(real, "VENUES", {"kalshi": vf.KALSHI})
    monkeypatch.setattr(real, "VENUE", "kalshi")


RULES = vf.KALSHI.exchange
AFTER = vf.SLOT + timedelta(minutes=1)


def run(
    record: lb.R2Store,
    raw: Any,
    client: Any,
    clock: vf.Clock,
    *,
    slot: rv.Snapshot | None = None,
    odds: Any = None,
) -> rv.RevalueReport:
    parts, missing = rv.resting(record, pm.LIVE, rv.ledger_of(record), clock())
    assert missing == 0
    assert RULES is not None
    return rv.revalue(record, raw, client, pf.live(), RULES, parts, clock, slot=slot, odds=odds)


def slot_run(
    tmp_path: Path, prices: tuple[float, float], updated: datetime | None = None
) -> tuple[lb.R2Store, Exchange, rv.RevalueReport]:
    record, raw, exchange, client, clock = vf.placed(tmp_path)
    body = vf.odds_body({2026020053: prices}, updated or vf.SLOT - timedelta(seconds=20))
    key = vf.store_slot(raw, vf.SLOT, body)
    clock.at = AFTER
    report = run(record, raw, client, clock, slot=rv.Snapshot(body, vf.SLOT, key))
    return record, exchange, report


# The value and the rule


def test_the_value_is_the_frozen_blend_at_pinnacles_fresh_pair_with_the_decisions_b3_and_u(
    tmp_path: Path,
) -> None:
    record, _, _ = slot_run(tmp_path, (2.20, 1.72))
    valued = vf.records(record)[-1]
    ledger = rv.ledger_of(record)(pf.DAY)
    assert ledger is not None
    row = ledger.filter(pl.col("game_id") == 2026020053).row(0, named=True)
    p_b0 = devig(pl.DataFrame({"home_price": [2.20], "away_price": [1.72]})).to_numpy()
    expected = pf.live().blends["BLEND"].predict(p_b0, [row["p_b3"]], [row["u"]])[0]
    assert valued["p_side"] == pytest.approx(expected, abs=1e-6)  # WSH is the home side
    # the decision's own price gives back the decision's p
    unchanged, _, _ = slot_run(tmp_path / "same", vf.DECISION_PRICES[2026020053])
    assert vf.records(unchanged)[-1]["p_side"] == pytest.approx(row["p_side"], abs=1e-6)


def test_a_move_against_the_bid_lowers_it_to_the_highest_cent_that_clears(tmp_path: Path) -> None:
    record, exchange, report = slot_run(tmp_path, (2.20, 1.72))
    assert report.lowered == 1 and report.valued == 1
    valued = vf.records(record)[-1]
    assert (valued["action"], valued["trigger"]) == (rv.LOWER, rv.SLOT)
    assert (valued["limit_before"], valued["limit_after"]) == ("0.49", "0.48")
    assert exchange.orders[vf.WSH_MAKER]["yes_price_dollars"] == "0.4800"
    # the amend keeps the count: filled plus resting
    order_id, body = exchange.amends[0]
    assert order_id == vf.WSH_MAKER and body["count"] == valued["remaining"]
    # 48 cents clears the hurdle at p', 49 doesn't
    assert RULES is not None
    count, p = Decimal(valued["remaining"]), valued["p_side"]
    for cents, clears in ((48, True), (49, False)):
        total = cost(count, Decimal(cents) / 100, RULES, maker=True)
        assert (p * float(count / total) - 1 >= 0.025) is clears


def test_a_bid_is_never_raised(tmp_path: Path) -> None:
    record, exchange, report = slot_run(tmp_path, (1.95, 1.93))
    valued = vf.records(record)[-1]
    assert valued["p_side"] > 0.5164
    assert valued["action"] == rv.REST and valued["limit_after"] == "0.49"
    assert report.rested == 1 and not exchange.amends


def test_no_cent_that_clears_cancels_the_bid() -> None:
    assert RULES is not None
    assert rv.lowered(0.005, 0.025, Decimal("0.49"), Decimal("2.45"), RULES) is None
    assert rv.lowered(0.40, 0.025, Decimal("0.49"), Decimal("2.45"), RULES) == Decimal("0.38")
    assert rv.lowered(0.60, 0.025, Decimal("0.49"), Decimal("2.45"), RULES) == Decimal("0.49")


def test_a_bid_no_cent_clears_for_is_cancelled_on_kalshi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rv, "lowered", lambda *args: None)
    record, exchange, report = slot_run(tmp_path, (2.60, 1.52))
    assert report.cancelled == 1
    assert exchange.orders[vf.WSH_MAKER]["status"] == "canceled"
    assert vf.records(record)[-1]["action"] == rv.CANCEL
    parts, _ = rv.resting(record, pm.LIVE, rv.ledger_of(record), AFTER)
    assert 2026020053 not in {p.game_id for p in parts}


def test_a_quote_older_than_five_minutes_is_no_valuation(tmp_path: Path) -> None:
    record, exchange, report = slot_run(tmp_path, (2.60, 1.52), vf.SLOT - timedelta(minutes=6))
    assert report.stale == 3 and report.valued == 0 and not exchange.amends
    assert vf.records(record)[-1]["action"] == rv.STALE


def test_a_snapshot_the_decision_read_before_the_order_went_values_nothing(
    tmp_path: Path,
) -> None:
    record, raw, _exchange, client, clock = vf.placed(tmp_path)
    taken = vf.PLACED_AT - timedelta(minutes=8)
    body = vf.odds_body({2026020053: (2.60, 1.52)}, taken)
    key = vf.store_slot(raw, taken, body, "midday")
    report = run(record, raw, client, clock, slot=rv.Snapshot(body, taken, key))
    assert report.valued == report.stale == 0 and not vf.records(record)


def test_a_lowered_bid_rests_at_its_new_limit_for_the_next_valuation(tmp_path: Path) -> None:
    record, exchange, _ = slot_run(tmp_path, (2.20, 1.72))
    parts, _ = rv.resting(record, pm.LIVE, rv.ledger_of(record), AFTER)
    wsh = next(p for p in parts if p.game_id == 2026020053)
    assert wsh.limit == Decimal("0.48") and wsh.last is not None
    # back to the decision's price: it stays at 48 cents, never raised again
    raw = vf.RawStore(tmp_path / "raw")
    later = vf.SLOT + timedelta(minutes=30)  # before the 23:25 expiry
    body = vf.odds_body({2026020053: vf.DECISION_PRICES[2026020053]}, later - timedelta(seconds=10))
    key = vf.store_slot(raw, later, body, "pre8")
    clock = vf.Clock(later + timedelta(minutes=1))
    client, _ = vf.trade(tmp_path / "again", exchange, env=pm.LIVE, now=clock)
    run(record, raw, client, clock, slot=rv.Snapshot(body, later, key))
    assert vf.records(record)[-1]["action"] == rv.REST
    assert exchange.orders[vf.WSH_MAKER]["yes_price_dollars"] == "0.4800"


# The starters


def test_a_starter_change_since_the_decision_cancels_the_bid_without_a_price(
    tmp_path: Path,
) -> None:
    record, raw, exchange, client, clock = vf.placed(tmp_path)
    vf.store_page(raw, pf.DECISION - timedelta(minutes=2), {"WSH": 1, "PIT": 2})
    vf.store_page(raw, AFTER - timedelta(minutes=1), {"WSH": 7, "PIT": 2})
    clock.at = AFTER
    calls: list[httpx.Request] = []
    report = run(record, raw, client, clock, odds=vf.odds_api(b"[]", clock, calls))
    assert report.starters == 1 and report.cancelled == 1 and not calls
    assert exchange.orders[vf.WSH_MAKER]["status"] == "canceled"
    valued = vf.records(record)[-1]
    assert (valued["trigger"], valued["action"]) == (rv.STARTER, rv.CANCEL)
    assert valued["starters"] == {"decision": {"WSH": 1, "PIT": 2}, "now": {"WSH": 7, "PIT": 2}}


def test_a_starter_first_named_after_the_decision_is_no_change(tmp_path: Path) -> None:
    record, raw, exchange, client, clock = vf.placed(tmp_path)
    vf.store_page(raw, pf.DECISION - timedelta(minutes=2), {"WSH": None, "PIT": 2})
    vf.store_page(raw, AFTER - timedelta(minutes=1), {"WSH": 7, "PIT": 2})
    clock.at = AFTER
    report = run(record, raw, client, clock)
    assert report.starters == 0 and exchange.orders[vf.WSH_MAKER]["status"] == "resting"


def test_the_decisions_starters_are_the_page_stored_before_it(tmp_path: Path) -> None:
    # A page stored after the decision, naming other starters, is never the decision's.
    record, raw, _exchange, client, clock = vf.placed(tmp_path)
    vf.store_page(raw, pf.DECISION + timedelta(minutes=1), {"WSH": 1, "PIT": 2})
    vf.store_page(raw, AFTER - timedelta(minutes=1), {"WSH": 7, "PIT": 2})
    clock.at = AFTER
    assert run(record, raw, client, clock).starters == 0


# Kalshi's move and the Odds API


def moved_setup(
    tmp_path: Path, ask_now: str = "0.5500", age: timedelta = timedelta(minutes=1)
) -> tuple[lb.R2Store, Any, Exchange, Any, vf.Clock]:
    record, raw, exchange, client, clock = vf.placed(tmp_path)
    vf.store_tops(raw, vf.PLACED_AT - timedelta(minutes=1), {vf.WSH: ("0.4500", "0.5200")})
    clock.at = AFTER
    vf.store_tops(raw, AFTER - age, {vf.WSH: ("0.4500", ask_now)})
    return record, raw, exchange, client, clock


def test_a_kalshi_move_of_three_cents_claims_one_call_and_values_that_part(
    tmp_path: Path,
) -> None:
    record, raw, _exchange, client, clock = moved_setup(tmp_path)
    calls: list[httpx.Request] = []
    body = vf.odds_body({2026020053: (2.20, 1.72)}, AFTER - timedelta(seconds=30))
    report = run(record, raw, client, clock, odds=vf.odds_api(body, clock, calls))
    assert report.moved == 1 and report.calls == 1 and len(calls) == 1
    assert calls[0].url.params["markets"] == "h2h" and calls[0].url.params["regions"] == "eu"
    valued = vf.records(record)[-1]
    assert (valued["trigger"], valued["action"]) == (rv.MOVED, rv.LOWER)
    assert valued["top"]["yes_ask"] == 0.55
    # the claim is kept, and the response is stored apart from the slots' odds
    assert record.keys(f"{rv.CALLS}/{pf.DAY.isoformat()}")
    assert raw.latest(f"{rv.SOURCE}/{AFTER:%Y-%m-%d}") is not None
    assert raw.latest(f"odds/{AFTER:%Y-%m-%d}") is None
    # the other games didn't move, so they aren't valued
    assert not vf.records(record, 2026020054)


def test_a_move_under_three_cents_triggers_nothing(tmp_path: Path) -> None:
    record, raw, _, client, clock = moved_setup(tmp_path, ask_now="0.5400")
    calls: list[httpx.Request] = []
    report = run(record, raw, client, clock, odds=vf.odds_api(b"[]", clock, calls))
    assert report.moved == 0 and not calls


def test_a_stale_top_of_book_triggers_nothing(tmp_path: Path) -> None:
    record, raw, _, client, clock = moved_setup(tmp_path, age=rv.TOP_AGE + timedelta(minutes=1))
    calls: list[httpx.Request] = []
    assert run(record, raw, client, clock, odds=vf.odds_api(b"[]", clock, calls)).moved == 0


def test_the_next_move_is_measured_from_the_last_valuation(tmp_path: Path) -> None:
    record, raw, _exchange, client, clock = moved_setup(tmp_path)
    calls: list[httpx.Request] = []
    body = vf.odds_body({2026020053: (2.20, 1.72)}, AFTER - timedelta(seconds=30))
    run(record, raw, client, clock, odds=vf.odds_api(body, clock, calls))
    # 15 minutes later the ask is where the last valuation saw it: no move, no call
    clock.at = AFTER + timedelta(minutes=15)
    vf.store_tops(raw, clock.at - timedelta(minutes=1), {vf.WSH: ("0.4500", "0.5600")})
    assert run(record, raw, client, clock, odds=vf.odds_api(body, clock, calls)).moved == 0
    assert len(calls) == 1


def test_the_days_calls_stop_at_four(tmp_path: Path) -> None:
    record, raw, _, client, clock = moved_setup(tmp_path)
    for n in range(1, rv.CALLS_PER_DAY + 1):
        lb.put_once(record, f"{rv.CALLS}/{pf.DAY.isoformat()}/{n}.json", b"{}")
    calls: list[httpx.Request] = []
    report = run(record, raw, client, clock, odds=vf.odds_api(b"[]", clock, calls))
    assert report.moved == 1 and report.waiting == 1 and report.calls == 0 and not calls
    assert not vf.records(record)


def test_two_runs_never_claim_the_same_call(tmp_path: Path) -> None:
    record, *_ = vf.placed(tmp_path)
    now = datetime(2026, 10, 7, 23, tzinfo=UTC)
    claimed = [rv.claim_call(record, pf.DAY, now) for _ in range(rv.CALLS_PER_DAY + 2)]
    assert claimed == [True] * rv.CALLS_PER_DAY + [False, False]
    assert len(record.keys(f"{rv.CALLS}/{pf.DAY.isoformat()}")) == rv.CALLS_PER_DAY


# The record, the messages and the reconcile


def test_each_valuation_is_written_once_with_what_it_read(tmp_path: Path) -> None:
    record, _, _ = slot_run(tmp_path, (2.20, 1.72))
    valued = vf.records(record)[-1]
    assert set(valued) >= {
        "valued_utc",
        "trigger",
        "action",
        "p_side",
        "limit_before",
        "limit_after",
        "remaining",
        "order_id",
        "quote",
        "top",
    }
    assert valued["quote"]["home_price"] == 2.20
    assert valued["quote"]["raw_key"].startswith("odds/2026-10-07/")


def test_the_owner_hears_of_each_bid_lowered_or_cancelled_once(tmp_path: Path) -> None:
    record, _, _ = slot_run(tmp_path, (2.20, 1.72))
    told = rv.unnotified(record, pm.LIVE)
    text = rv.message(pm.LIVE, told)
    assert text is not None and "2026-10-07 WSH: bid lowered from 49¢ to 48¢" in text
    rv.mark_notified(record, pm.LIVE, told, AFTER)
    assert rv.unnotified(record, pm.LIVE) == []
    assert rv.message(pm.LIVE, []) is None


def test_the_reconcile_takes_every_fill_of_a_lowered_bid(tmp_path: Path) -> None:
    exchange = Exchange()
    exchange.new_ids = True  # Kalshi answers the amend with another order
    record, raw, exchange, client, clock = vf.placed(tmp_path, exchange)
    exchange.fill(vf.WSH_MAKER, "0.50", "0.4900", "0.0022", vf.PLACED_AT + timedelta(hours=1))
    body = vf.odds_body({2026020053: (2.20, 1.72)}, vf.SLOT - timedelta(seconds=20))
    key = vf.store_slot(raw, vf.SLOT, body)
    clock.at = AFTER
    run(record, raw, client, clock, slot=rv.Snapshot(body, vf.SLOT, key))
    lowered = vf.records(record)[-1]
    assert lowered["order_id"] != vf.WSH_MAKER
    exchange.fill(lowered["order_id"], "1.00", "0.4800", "0.0044", AFTER + timedelta(minutes=5))
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=4)
    pm.reconcile(record, client, clock, vf.STARTS)
    final = json.loads(record.get(pm.order_key(pm.LIVE, pf.DAY, 2026020053, "final")))
    assert final["parts"][pm.MAKER]["contracts"] == "1.50"
    assert len(final["maker_orders"]) == 2


def test_a_part_kalshi_no_longer_shows_resting_is_left_to_the_reconcile(tmp_path: Path) -> None:
    record, raw, exchange, client, clock = vf.placed(tmp_path)
    exchange.orders[vf.WSH_MAKER]["status"] = "executed"
    body = vf.odds_body({2026020053: (2.60, 1.52)}, vf.SLOT - timedelta(seconds=20))
    key = vf.store_slot(raw, vf.SLOT, body)
    clock.at = AFTER
    run(record, raw, client, clock, slot=rv.Snapshot(body, vf.SLOT, key))
    assert not vf.records(record) and not exchange.amends


def test_nothing_rests_once_the_expiry_is_near(tmp_path: Path) -> None:
    record, *_ = vf.placed(tmp_path)
    near = pf.START - timedelta(minutes=5) - pm.LEAD
    parts, _ = rv.resting(record, pm.LIVE, rv.ledger_of(record), near)
    assert {p.game_id for p in parts} == {2026020055}


# The command


runner = CliRunner()


def command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str | None
) -> tuple[lb.R2Store, Any, Exchange, list[str], vf.Clock]:
    """nhl real revalue wired to the placed day's record, its raw store, the fake exchange and a
    message catcher, at AFTER."""
    from nhl_edge import cli
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.live import kalshi_trade as kt
    from nhl_edge.live import telegram

    record, raw, exchange, _, clock = vf.placed(tmp_path)
    clock.at = AFTER
    sent: list[str] = []
    monkeypatch.setattr(cli, "_real_record", lambda r2: (None, record))
    monkeypatch.setattr(telegram, "send", lambda text: sent.append(text))
    monkeypatch.setattr("nhl_edge.settings.load_env", lambda: None)
    monkeypatch.setattr(RawStore, "from_env", classmethod(lambda cls, *a, **k: raw))
    clients = itertools.count()
    monkeypatch.setattr(
        kt.KalshiTrade,
        "from_env",
        classmethod(
            lambda cls, env, store: vf.trade(
                tmp_path / f"cli-{next(clients)}", exchange, env=env, now=clock
            )[0]
        ),
    )

    class Day(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return clock()

    monkeypatch.setattr(cli, "datetime", Day)
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    if setting is None:
        monkeypatch.delenv("KALSHI_TRADING", raising=False)
    else:
        monkeypatch.setenv("KALSHI_TRADING", setting)
    return record, raw, exchange, sent, clock


def test_nhl_real_revalue_with_the_switch_off_does_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, exchange, sent, _ = command(tmp_path, monkeypatch, None)
    before = len(exchange.requests)
    result = runner.invoke(app, ["real", "revalue", "--r2", "--slot"])
    assert result.exit_code == 0, result.output
    assert "KALSHI_TRADING is off, nothing re-valued" in result.output
    assert len(exchange.requests) == before and not sent


def test_nhl_real_revalue_at_a_slot_lowers_and_prints_counts_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record, raw, _exchange, sent, clock = command(tmp_path, monkeypatch, "live")
    body = vf.odds_body({2026020053: (2.20, 1.72)}, vf.SLOT - timedelta(seconds=20))
    vf.store_slot(raw, vf.SLOT, body)
    result = runner.invoke(app, ["real", "revalue", "--r2", "--slot"])
    assert result.exit_code == 0, result.output
    assert "kalshi revalue live: 3 resting" in result.output and "1 lowered" in result.output
    for secret in (vf.WSH, vf.WSH_MAKER, "¢", "$", "0.48"):
        assert secret not in result.output
    assert len(sent) == 1 and "WSH: bid lowered from 49¢ to 48¢" in sent[0]
    # told once
    clock.at += timedelta(minutes=1)
    assert runner.invoke(app, ["real", "revalue", "--r2", "--slot"]).exit_code == 0
    assert len(sent) == 1


def test_nhl_real_revalue_at_a_slot_without_its_snapshot_values_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, raw, exchange, sent, _ = command(tmp_path, monkeypatch, "live")
    old = vf.SLOT - timedelta(minutes=30)
    vf.store_slot(raw, old, vf.odds_body({2026020053: (2.60, 1.52)}, old))
    result = runner.invoke(app, ["real", "revalue", "--r2", "--slot"])
    assert result.exit_code == 0, result.output
    assert "no slot snapshot in the last 15 minutes" in result.output
    assert not exchange.amends and not sent


def test_nhl_real_revalue_between_slots_waits_without_an_odds_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A move with no Odds API key set: the trigger waits for the next slot, and the run polls
    # Daily Faceoff for the starters itself.
    from nhl_edge.ingest import dailyfaceoff as dfo

    _, raw, exchange, sent, _ = command(tmp_path, monkeypatch, "live")
    vf.store_tops(raw, vf.PLACED_AT - timedelta(minutes=1), {vf.WSH: ("0.4500", "0.5200")})
    vf.store_tops(raw, AFTER - timedelta(minutes=1), {vf.WSH: ("0.4500", "0.5600")})
    polled: list[Any] = []
    monkeypatch.setattr(dfo.DailyFaceoff, "starting_goalies", lambda self, day: polled.append(day))
    result = runner.invoke(app, ["real", "revalue", "--r2"])
    assert result.exit_code == 0, result.output
    assert "1 Kalshi moves" in result.output and "1 waiting for the next slot" in result.output
    assert polled == [pf.DAY] and not exchange.amends and not sent
