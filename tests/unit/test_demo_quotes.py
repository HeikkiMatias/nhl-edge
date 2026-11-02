"""Two-sided demo quoting (#375, ADR 0055) against a fake exchange: the quotes, re-quoting, the
caps, the pulls and holds, the stop before the start, refusals, the recovery of an earlier run's
fills and the record across runs. SYNTHETIC responses only: the repository holds no Kalshi response
(#280)."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from nhl_edge.ingest.kalshi import ticker
from nhl_edge.ingest.odds import ET
from nhl_edge.live import bundle as lb
from nhl_edge.live import demo_quotes as dq
from nhl_edge.live import kalshi_quotes as kq
from nhl_edge.live.kalshi_trade import KalshiTradeError, OrderRequest, Placed
from nhl_edge.live.real import DEMO_EXCHANGE

DAY = date(2026, 11, 4)
START = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)  # 19:00 EST
AT = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)
PUBLISHED = AT - timedelta(minutes=30)
GAME = kq.Game(2026020150, 20262027, DAY, "BOS", "NYR", START)
HOME = ticker(DAY, "NYR", "BOS", "BOS")
AWAY = ticker(DAY, "NYR", "BOS", "NYR")


@dataclass
class Fake:
    """A demo exchange: each market's top, the orders sent, and refusals on demand."""

    tops: dict[str, tuple[str | None, str | None]] = field(
        default_factory=lambda: {HOME: ("0.53", "0.56"), AWAY: ("0.43", "0.46")}
    )
    orders_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    sent: list[OrderRequest] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    refuse_cancel: bool = False
    refuse_create: bool = False

    def market(self, ticker: str) -> dict[str, Any]:
        bid, ask = self.tops[ticker]
        return {"status": "active", "yes_bid_dollars": bid, "yes_ask_dollars": ask}

    def create_order(self, order: OrderRequest) -> Placed:
        if self.refuse_create:
            raise KalshiTradeError("create-order: status 403", 403, "trading_not_allowed")
        self.sent.append(order)
        order_id = f"o{len(self.sent)}"
        self.orders_by_id[order_id] = {
            "order_id": order_id,
            "client_order_id": order.client_order_id,
            "ticker": order.ticker,
            "status": "resting",
            "fill_count_fp": "0.00",
            "yes_price_dollars": f"{order.price:.4f}",
        }
        return Placed(order_id, order.client_order_id, False, {})

    def order(self, order_id: str) -> dict[str, Any]:
        return self.orders_by_id[order_id]

    def cancel(self, order_id: str, ticker: str) -> dict[str, Any] | None:
        if self.refuse_cancel:
            raise KalshiTradeError("cancel-order: status 403", 403)
        self.orders_by_id[order_id]["status"] = "canceled"
        self.cancelled.append(order_id)
        return {}

    def orders(self, ticker: str) -> list[dict[str, Any]]:
        return [o for o in self.orders_by_id.values() if o["ticker"] == ticker]

    def fill(self, order_id: str, contracts: str) -> None:
        self.orders_by_id[order_id]["fill_count_fp"] = contracts


def sources(
    p_home: float = 0.55,
    published: datetime = PUBLISHED,
    named: dict[str, int | None] | None = None,
) -> dq.Sources:
    return dq.Sources(
        fair=lambda game, at: (p_home, published) if published < at else None,
        named=lambda game, at: named,
    )


def day() -> dq.Day:
    return dq.Day(DAY, "130000")


def step(state: dq.Day, fake: Fake, at: datetime = AT, **changes: Any) -> None:
    dq.step(state, [GAME], at, sources(**changes), fake, DEMO_EXCHANGE)


def resting(fake: Fake) -> list[dict[str, Any]]:
    return [o for o in fake.orders_by_id.values() if o["status"] == "resting"]


def test_both_teams_get_a_post_only_bid_a_cent_below_the_fair_value() -> None:
    fake, state = Fake(), day()
    step(state, fake)
    assert [o.ticker for o in fake.sent] == [HOME, AWAY]
    home, away = fake.sent
    # The simulator's own price rule, a float fair value floored to the cent (#376 tracks it).
    assert home.price == Decimal("0.54")
    assert away.price == kq._quote_price(1 - 0.55, dq.HALF_SPREAD, Decimal("0.46"))
    assert home.count == dq.SIZE and home.post_only
    assert home.expiration_time == START - dq.STOP_BEFORE
    assert home.client_order_id.startswith("q-20261104-2026020150-BOS-130000-")
    # Nothing changed: the bids rest as they are.
    step(state, fake, AT + dq.STEP)
    assert len(fake.sent) == 2 and fake.cancelled == []


def test_a_bid_stays_a_cent_below_the_best_ask() -> None:
    fake, state = Fake(), day()
    fake.tops[HOME] = ("0.52", "0.54")
    step(state, fake)
    assert fake.sent[0].price == Decimal("0.53")


def test_a_fill_counts_and_the_net_cap_sizes_the_next_bid() -> None:
    fake, state = Fake(), day()
    step(state, fake)
    fake.fill("o1", "10.00")
    fake.orders_by_id["o1"]["status"] = "executed"
    step(state, fake, AT + dq.STEP)
    assert state.bought[(GAME.game_id, "BOS")] == Decimal(10)
    assert state.filled_cost > Decimal("5.39")
    # 20 more fill: the net is 30 against none, so no bid is left to post on BOS.
    fake.fill("o3", "10.00")
    fake.orders_by_id["o3"]["status"] = "executed"
    step(state, fake, AT + 2 * dq.STEP)
    fake.fill("o4", "10.00")
    fake.orders_by_id["o4"]["status"] = "executed"
    step(state, fake, AT + 3 * dq.STEP)
    assert state.bought[(GAME.game_id, "BOS")] == Decimal(30)
    assert all(o["ticker"] != HOME for o in resting(fake))
    assert any(o["ticker"] == AWAY for o in resting(fake))


def test_a_starter_change_pulls_the_game_until_the_fair_value_updates() -> None:
    fake, state = Fake(), day()
    step(state, fake, named={"BOS": 1, "NYR": 2})
    step(state, fake, AT + dq.STEP, named={"BOS": 3, "NYR": 2})
    assert state.counts["pulled"] == 1 and resting(fake) == []
    assert state.held[GAME.game_id][1] == dq.STARTER
    # The same fair value: still held.
    step(state, fake, AT + 2 * dq.STEP, named={"BOS": 3, "NYR": 2})
    assert resting(fake) == []
    # A newer fair value releases it.
    later = AT + 2 * dq.STEP
    step(state, fake, AT + 3 * dq.STEP, named={"BOS": 3, "NYR": 2}, published=later)
    assert len(resting(fake)) == 2 and GAME.game_id not in state.held


def test_a_three_cent_move_pulls_the_game() -> None:
    fake, state = Fake(), day()
    step(state, fake)
    fake.tops[HOME] = ("0.50", "0.53")
    step(state, fake, AT + dq.STEP)
    assert state.held[GAME.game_id][1] == dq.MOVE and resting(fake) == []


def test_every_bid_is_cancelled_ten_minutes_before_the_start() -> None:
    fake, state = Fake(), day()
    step(state, fake)
    step(state, fake, START - dq.STOP_BEFORE)
    assert resting(fake) == [] and len(fake.sent) == 2


def test_the_day_cap_stops_new_bids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dq, "DAY_CAP", Decimal(8))
    fake, state = Fake(), day()
    step(state, fake)
    # The home bid costs about 5.4 with the fee; the away one would pass 8.
    assert [o.ticker for o in fake.sent] == [HOME]
    assert state.counts["capped"] == 1


def test_a_refused_cancel_is_tried_again_and_a_refused_post_recorded() -> None:
    fake, state = Fake(), day()
    step(state, fake)
    fake.refuse_cancel = True
    step(state, fake, START - dq.STOP_BEFORE)
    assert state.counts["cancel_refused"] == 2 and state.refused_in_a_row == 2
    assert len(state.stuck) == 2
    fake.refuse_cancel = False
    step(state, fake, START - dq.STOP_BEFORE + dq.STEP)
    assert resting(fake) == [] and state.stuck == [] and state.refused_in_a_row == 0
    other = day()
    fake.refuse_create = True
    step(other, Fake(refuse_create=True))
    assert other.counts["post_refused"] == 2 and other.bids == {}


def test_a_new_run_recovers_the_days_fills_and_cancels_what_rests() -> None:
    fake, first = Fake(), day()
    step(first, fake)
    fake.fill("o1", "4.00")
    # Another order on the market, not a demo quote, is left alone.
    fake.orders_by_id["x"] = {
        "order_id": "x",
        "client_order_id": "demo-practice-1",
        "ticker": HOME,
        "status": "resting",
        "fill_count_fp": "7.00",
        "yes_price_dollars": "0.5000",
    }
    second = dq.Day(DAY, "150500")
    dq.recover(second, fake, [GAME], AT + timedelta(hours=2), DEMO_EXCHANGE)
    assert second.bought == {(GAME.game_id, "BOS"): Decimal(4)}
    assert sorted(fake.cancelled) == ["o1", "o2"]
    assert fake.orders_by_id["x"]["status"] == "resting"


def test_the_record_carries_holds_and_starters_to_the_next_run(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    fake, state = Fake(), day()
    step(state, fake, named={"BOS": 1, "NYR": 2})
    step(state, fake, AT + dq.STEP, named={"BOS": 3, "NYR": 2})
    dq.cancel_all(state, fake, AT + 2 * dq.STEP, DEMO_EXCHANGE)
    key = dq.keep(store, state, AT + 2 * dq.STEP)
    assert key == "real/demo/quotes/2026-11-04/130000.json"
    later = dq.resumed(store, DAY, "150500")
    assert later.held[GAME.game_id][1] == dq.STARTER
    assert later.named[GAME.game_id] == {"BOS": 3, "NYR": 2}
    assert dq.summary(dq.runs(store, DAY))["posted"] == 2
    assert "BOS" not in dq.count_line(state) and "0.5" not in dq.count_line(state)


def test_the_kill_switch_stops_the_day_and_cancels_only_its_quotes(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    fake, state = Fake(), day()
    step(state, fake)
    fake.orders_by_id["x"] = {
        "order_id": "x",
        "client_order_id": "demo-practice-1",
        "ticker": HOME,
        "status": "resting",
        "fill_count_fp": "0.00",
        "yes_price_dollars": "0.5000",
    }
    assert not dq.stopped(store, DAY)
    dq.stop(store, DAY, AT)
    dq.stop(store, DAY, AT + dq.STEP)
    assert dq.stopped(store, DAY) and not dq.stopped(store, DAY + timedelta(days=1))
    assert dq.cancel_resting(fake, [GAME], DAY, AT) == (2, 0)
    assert fake.orders_by_id["x"]["status"] == "resting"
    assert dq.runs(store, DAY) == []


def test_the_fair_value_is_the_morning_blend_then_the_decisions(tmp_path: Path) -> None:
    from nhl_edge.live import morning as lm
    from nhl_edge.live.predict import ledger_key

    store = lb.LocalStore(tmp_path)
    fair = dq.fair_reader(store, DAY)
    assert fair(GAME, AT) is None

    def rows(p: float, published: datetime) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "game_id": [GAME.game_id],
                "status": ["predicted"],
                "p_blend": [p],
                "published_utc": [published],
            }
        ).with_columns(pl.col("published_utc").cast(pl.Datetime("us", "UTC")))

    store.put(lm.key(DAY), lb.parquet(rows(0.55, PUBLISHED)))
    assert fair(GAME, AT) == (0.55, PUBLISHED)
    decided = AT + timedelta(hours=4)
    store.put(ledger_key(DAY), lb.parquet(rows(0.6, decided)))
    assert fair(GAME, AT) == (0.55, PUBLISHED)
    assert fair(GAME, decided + timedelta(seconds=1)) == (0.6, decided)


def test_the_command_quotes_only_on_the_demo_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app

    for setting in ("off", "live"):
        monkeypatch.setenv("KALSHI_TRADING", setting)
        result = CliRunner().invoke(app, ["real", "quote"])
        assert result.exit_code == 0
        assert f"KALSHI_TRADING is {setting}, not demo: nothing quoted" in result.output


def test_a_bid_whose_cancel_is_refused_still_counts_and_blocks_its_replacement() -> None:
    fake, state = Fake(), day()
    step(state, fake)
    fake.refuse_cancel = True
    # A new fair value would replace both bids, but their cancels are refused.
    step(state, fake, AT + dq.STEP, p_home=0.6, published=AT)
    assert len(state.stuck) == 2 and state.bids == {}
    assert len(fake.sent) == 2 and state.counts["waiting_on_cancel"] == 2
    assert state.resting_cost(DEMO_EXCHANGE) > Decimal(9)
    # A stuck bid's fills are still read, and once it is no longer resting it is let go.
    fake.fill("o1", "10.00")
    fake.orders_by_id["o1"]["status"] = "executed"
    step(state, fake, AT + 2 * dq.STEP, p_home=0.6, published=AT)
    assert state.bought[(GAME.game_id, "BOS")] == Decimal(10)
    assert [b.order_id for b in state.stuck] == ["o2"]


def test_a_fill_just_before_a_cancel_counts() -> None:
    fake, state = Fake(), day()
    step(state, fake)
    # Filled after the step's read, then cancelled at the stop: the cancel's last read counts it.
    original = fake.cancel

    def fill_then_cancel(order_id: str, ticker: str) -> dict[str, Any] | None:
        if order_id == "o1":
            fake.fill("o1", "3.00")
        return original(order_id, ticker)

    fake.cancel = fill_then_cancel  # type: ignore[method-assign]
    step(state, fake, START - dq.STOP_BEFORE)
    assert state.bought[(GAME.game_id, "BOS")] == Decimal(3)
    assert state.counts["filled"] == 1


def test_a_poll_naming_no_starter_keeps_the_last_one_named() -> None:
    fake, state = Fake(), day()
    step(state, fake, named={"BOS": 1, "NYR": 2})
    step(state, fake, AT + dq.STEP, named={"BOS": None, "NYR": 2})
    assert state.named[GAME.game_id] == {"BOS": 1, "NYR": 2} and state.counts["pulled"] == 0
    step(state, fake, AT + 2 * dq.STEP, named={"BOS": 3, "NYR": 2})
    assert state.counts["pulled"] == 1 and state.held[GAME.game_id][1] == dq.STARTER


def test_the_days_runs_read_back_in_order_across_utc_midnight(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    for run, game_id, goalie in (("20261105T000500Z", 2, 9), ("20261104T200500Z", 2, 5)):
        state = dq.Day(DAY, run)
        state.named[game_id] = {"BOS": goalie, "NYR": None}
        dq.keep(store, state, AT)
    # The run after UTC midnight is the later one: its starters stand.
    assert [r["run"] for r in dq.runs(store, DAY)] == ["20261104T200500Z", "20261105T000500Z"]
    assert dq.resumed(store, DAY, "x").named[2] == {"BOS": 9, "NYR": None}


def test_the_kill_switch_works_with_trading_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from nhl_edge import cli
    from nhl_edge.live import kalshi_trade as kt

    store = lb.LocalStore(tmp_path)
    fake = Fake()
    monkeypatch.setenv("KALSHI_TRADING", "off")
    monkeypatch.setattr(cli, "_real_record", lambda r2: (None, store))
    monkeypatch.setattr(kt.KalshiTrade, "from_env", classmethod(lambda cls, env, raw: fake))
    result = CliRunner().invoke(cli.app, ["real", "quote", "--cancel"])
    assert result.exit_code == 0, result.output
    today = datetime.now(UTC).astimezone(ET).date()
    assert dq.stopped(store, today)
    assert "stopped; 0 bids cancelled, 0 calls refused" in result.output


def test_a_fill_during_the_cancel_shrinks_or_stops_the_replacement() -> None:
    fake, state = Fake(), day()
    state.bought[(GAME.game_id, "BOS")] = Decimal(25)
    step(state, fake)
    assert fake.sent[0].ticker == HOME and fake.sent[0].count == Decimal(5)
    original = fake.cancel

    def fill_then_cancel(order_id: str, ticker: str) -> dict[str, Any] | None:
        if order_id == "o1":
            fake.fill("o1", "5.00")
        return original(order_id, ticker)

    fake.cancel = fill_then_cancel  # type: ignore[method-assign]
    # A newer fair value moves the price: the old BOS bid fills as it is cancelled, which takes
    # the net to 30, so no BOS replacement goes up.
    step(state, fake, AT + dq.STEP, p_home=0.6, published=AT)
    assert state.bought[(GAME.game_id, "BOS")] == Decimal(30)
    assert [o.ticker for o in fake.sent[2:]] == [AWAY]


def test_a_recovered_bid_whose_cancel_is_refused_keeps_what_still_rests() -> None:
    fake = Fake(refuse_cancel=True)
    fake.orders_by_id["o9"] = {
        "order_id": "o9",
        "client_order_id": "q-20261104-2026020150-BOS-20261104T120000Z-1",
        "ticker": HOME,
        "status": "resting",
        "fill_count_fp": "4.00",
        "remaining_count_fp": "6.00",
        "yes_price_dollars": "0.5400",
    }
    state = dq.Day(DAY, "20261104T160500Z")
    dq.recover(state, fake, [GAME], AT, DEMO_EXCHANGE)
    assert state.bought == {(GAME.game_id, "BOS"): Decimal(4)}
    (stuck,) = state.stuck
    assert stuck.left == Decimal(6) and state.resting_cost(DEMO_EXCHANGE) > Decimal(3)
