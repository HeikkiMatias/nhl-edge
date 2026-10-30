"""Shadow kalshi_quotes (#358 step 1): two-sided quotes from three fair values on SYNTHETIC logs:
the quote's price, its fills by the queue rule, the limits, the pulls, the end before the start,
and the figures with counts only in the log."""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import polars as pl
import pytest
import quotes_fixtures as qf

from nhl_edge.live import kalshi_quotes as kq
from nhl_edge.live import kalshi_shadow as ks
from nhl_edge.live.real import DEMO_EXCHANGE

NOW = qf.START + timedelta(days=1)


def day(**changes: object) -> kq.DayResult:
    args = {
        "ledger": qf.ledger(),
        "morning": qf.morning(),
        "odds": qf.odds(),
        "depth": qf.depth(),
        "trades": qf.trades([(10, qf.HOME, 0.51, 20.0)]),
        "tops": qf.tops(),
        "dailyfaceoff": qf.dailyfaceoff(),
        "games": qf.games(),
    } | changes
    return kq.run_day(qf.DAY, rules=DEMO_EXCHANGE, now=NOW, **args)  # type: ignore[arg-type]


def of(result: kq.DayResult, variant: str) -> pl.DataFrame:
    return result.fills.filter(pl.col("variant") == variant)


def test_a_quote_is_the_fair_value_less_the_half_spread_below_the_best_ask() -> None:
    cent = Decimal("0.01")
    assert kq._quote_price(0.55, Decimal("0.02"), Decimal("0.54")) == Decimal("0.53")
    # Never at or above the ask: a cent below it.
    assert kq._quote_price(0.55, Decimal("0.02"), Decimal("0.53")) == Decimal("0.52")
    assert kq._quote_price(0.556, Decimal("0.04"), None) == Decimal("0.51")
    assert kq._quote_price(0.02, Decimal("0.02"), None) is None
    assert kq._quote_price(0.03, Decimal("0.02"), None) == cent


def test_the_model_quotes_fill_by_the_queue_and_pay_the_maker_fee() -> None:
    result = day()
    # The home bid at 0.53 waits behind the 5 contracts ahead of it: the trade's 20 fill its 10.
    two = of(result, "model-2c")
    home = two.filter(pl.col("team") == qf.HOME)
    assert home["contracts"].to_list() == [10.0] and home["price"].to_list() == [0.53]
    assert home["fill_utc"].to_list() == [qf.at(10)]
    fee = Decimal("0.0175") * 10 * Decimal("0.53") * Decimal("0.47")
    expected = 10 * Decimal("0.53") + (fee / Decimal("0.0001")).to_integral_value(
        "ROUND_CEILING"
    ) * Decimal("0.0001")
    assert home["cost_usd"].to_list() == [float(expected)]
    assert home["fair_at_post"].to_list() == [pytest.approx(0.55)]
    # Above the ask, the fair value puts every half-spread at a cent below it: all three fill.
    assert of(result, "model-0c")["contracts"].sum() == 10.0
    assert of(result, "model-1c")["contracts"].sum() == 10.0
    # The away team's bid rests far below every trade.
    assert two.filter(pl.col("team") == qf.AWAY).is_empty()


def test_a_wider_half_spread_rests_further_below_the_trades() -> None:
    # A fair value of 0.52: the bids rest at 0.52, 0.51 and 0.50, and the trade at 0.51 fills two.
    result = day(ledger=qf.ledger(p_blend=0.52))
    prices = {
        cents: of(result, f"model-{cents}c").filter(pl.col("team") == qf.HOME)["price"].to_list()
        for cents in (0, 1, 2)
    }
    assert prices == {0: [0.52], 1: [0.51], 2: []}


def test_each_fill_is_valued_by_its_markouts_and_its_result() -> None:
    fill = of(day(), "model-2c").row(0, named=True)
    # The home market's mid stays 0.52 after the fill.
    assert fill["mid_5m"] == pytest.approx(0.52) and fill["mid_last"] is None
    assert fill["won"] == 1
    lost = of(day(games=qf.games(home_win=False)), "model-2c").row(0, named=True)
    assert lost["won"] == 0
    rows = kq.per_contract(of(day(), "model-2c"))
    assert rows["profit"].to_list()[0] == pytest.approx(1 - fill["cost_usd"] / 10)
    assert rows["spread"].to_list()[0] == pytest.approx(0.02)


def test_pinnacle_and_anchored_quote_from_their_own_fair_values() -> None:
    pair = [(qf.PUBLISHED - timedelta(minutes=5), 1.80, 2.10)]
    result = day(odds=qf.odds(pair))
    pinnacle = of(result, "pinnacle-2c").filter(pl.col("team") == qf.HOME)
    p_home = (1 / 1.80) / (1 / 1.80 + 1 / 2.10)
    assert pinnacle["fair_at_post"].to_list()[0] == pytest.approx(p_home)
    assert pinnacle["price"].to_list() == [0.51]
    # Anchored: halfway in log-odds between the model's 0.55 and the market's 0.52 mid.
    anchored = of(result, "anchored-2c").filter(pl.col("team") == qf.HOME)
    fair = anchored["fair_at_post"].to_list()[0]
    assert 0.52 < fair < 0.55
    price = kq._quote_price(fair, Decimal("0.02"), None)
    assert price is not None and anchored["price"].to_list() == [float(price)]


def test_a_team_is_quoted_only_to_the_net_cap() -> None:
    big = [(m, qf.HOME, 0.51, 100.0) for m in (10, 15, 20, 25, 30)]
    home = of(day(trades=qf.trades(big)), "model-2c").filter(pl.col("team") == qf.HOME)
    assert home["contracts"].sum() == float(kq.NET_CAP)


def test_a_partial_fill_never_takes_the_team_past_the_net_cap() -> None:
    # 10, 10 and then 5.5 of the third bid: the fourth bid is the 4.5 left to the cap.
    sizes = [(10, 20.0), (15, 20.0), (20, 10.5), (25, 100.0), (30, 100.0)]
    trades = qf.trades([(m, qf.HOME, 0.51, c) for m, c in sizes])
    result = day(trades=trades)
    home = of(result, "model-2c").filter(pl.col("team") == qf.HOME)
    assert home["contracts"].to_list() == [10.0, 10.0, 5.5, 4.5]
    assert result.counts["model-2c"]["largest_inventory"] == float(kq.NET_CAP)


def test_the_days_cost_cap_stops_new_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kq, "DAY_CAP", Decimal(12))
    big = [(m, qf.HOME, 0.51, 100.0) for m in range(10, 40, 5)]
    result = day(trades=qf.trades(big))
    # A quote of 10 at 0.53 costs 5.34, and the away bid rests too: the cap stops the third.
    home = of(result, "model-2c")
    assert 0 < home["cost_usd"].sum() <= 12
    assert home["contracts"].sum() < float(kq.NET_CAP)


def test_a_starter_change_pulls_the_game_until_its_fair_value_updates() -> None:
    polls = [
        (qf.at(-60), qf.HOME, "Logan Thompson"),
        (qf.at(5), qf.HOME, "Charlie Lindgren"),
    ]
    result = day(dailyfaceoff=qf.dailyfaceoff(polls))
    assert result.counts["model-2c"]["pulled_starter"] == 1
    # The model has no later publication, so the trade at minute 10 finds no bid.
    assert of(result, "model-2c").is_empty()
    # A later decision lets it quote again.
    later = pl.concat([qf.ledger(), qf.ledger(p_blend=0.56, published=qf.at(8))])
    again = day(
        ledger=later.tail(1),
        morning=qf.morning([(0.55, qf.PUBLISHED)]),
        dailyfaceoff=qf.dailyfaceoff(polls),
    )
    assert of(again, "model-2c").height == 1


def test_a_starter_change_with_no_bid_resting_still_holds_the_game() -> None:
    # The change comes after the decision and before the first top, while nothing rests.
    polls = [
        (qf.at(-60), qf.HOME, "Logan Thompson"),
        (qf.at(-5), qf.HOME, "Charlie Lindgren"),
    ]
    result = day(dailyfaceoff=qf.dailyfaceoff(polls))
    for name in ("model-2c", "anchored-2c"):
        assert result.counts[name]["posted"] == 0 and result.counts[name]["pulled_starter"] == 0
        assert of(result, name).is_empty()


def test_a_move_in_kalshis_mid_pulls_the_game() -> None:
    moved = qf.tops(moved={7: (0.54, 0.58)})
    result = day(tops=moved)
    assert result.counts["model-2c"]["pulled_move"] == 1
    # The model waits for its next publication; the anchored arm quotes again at the new mid.
    assert of(result, "model-2c").is_empty()
    assert result.counts["anchored-2c"]["posted"] > result.counts["model-2c"]["posted"]


def test_a_top_without_a_bid_at_the_moment_itself_is_found() -> None:
    series = [(qf.at(0), Decimal("0.50"), Decimal("0.54")), (qf.at(1), None, Decimal("0.54"))]
    assert kq.top_at(series, qf.at(1)) == series[1]
    assert kq.mid_of(kq.top_at(series, qf.at(1))) is None


def test_nothing_is_quoted_from_ten_minutes_before_the_start() -> None:
    start = qf.at(20)
    result = day(ledger=qf.ledger(start=start), trades=qf.trades([(12, qf.HOME, 0.51, 20.0)]))
    # The bid expired at minute 10, before the trade.
    assert of(result, "model-2c").is_empty()


def test_the_indexed_advance_is_kalshi_shadows() -> None:
    rng = random.Random(7)
    base = datetime(2026, 11, 3, 18, tzinfo=UTC)
    for _ in range(50):
        trades = tuple(
            sorted(
                (base + timedelta(minutes=rng.randint(0, 60)), Decimal(rng.randint(40, 60)) / 100,
                 Decimal(rng.randint(1, 30)))
                for _ in range(rng.randint(0, 15))
            )
        )  # fmt: skip
        asks = tuple(
            sorted(
                (base + timedelta(minutes=rng.randint(0, 60)), Decimal(rng.randint(45, 70)) / 100)
                for _ in range(rng.randint(0, 10))
            )
        )
        market = ks.Market((), trades, asks)

        def bid() -> ks.Bid:
            return ks.Bid(
                game_id=1, team="WSH", p_side=0.5, hurdle=0.0, price=Decimal("0.52"),
                left=Decimal(20), posted_utc=base, queue=Decimal(rng.randint(0, 10)),
                expires_utc=base + timedelta(minutes=50), placed_utc=base,
            )  # fmt: skip

        mine, theirs = bid(), bid()
        theirs.queue = mine.queue
        until = base + timedelta(minutes=rng.randint(0, 70))
        assert kq.advance(mine, until, kq.Logs.of(market), DEMO_EXCHANGE) == ks.advance(
            theirs, until, market, DEMO_EXCHANGE
        )
        assert (mine.left, mine.queue, mine.since) == (theirs.left, theirs.queue, theirs.since)


def test_the_summary_and_the_log_line(tmp_path: object) -> None:
    from pathlib import Path

    from nhl_edge.live import bundle as lb

    store = lb.LocalStore(Path(str(tmp_path)))
    result = day()
    kq.keep(store, qf.DAY, result, "kalshi-quotes-20261103-abc1234", NOW)
    found, metas = kq.kept(store)
    figures = kq.summary(found, metas)
    two = figures["model-2c"]
    assert two["fills"] == 1 and two["contracts"] == 10.0 and two["matched_contracts"] == 0.0
    # Under 4 weeks a measure has counts only.
    assert two["profit"] == {"games": 1, "weeks": 1}
    # Without Pinnacle's pair: the model and anchored arms each fill at every half-spread.
    line = kq.count_line(metas, 1)
    assert (
        line
        == "kalshi quotes: 1 days computed now, 1 kept (1 with watch logs), 6 fills over 9 variants"
    )
    assert kq.due(qf.DAY + timedelta(days=2), kq.stored_days(store)) == [
        d for d in kq.due(qf.DAY + timedelta(days=2), []) if d != qf.DAY
    ]
