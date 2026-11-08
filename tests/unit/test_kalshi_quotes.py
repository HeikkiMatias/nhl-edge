"""Shadow kalshi_quotes (#358 step 1): two-sided quotes from three fair values on SYNTHETIC logs:
the quote's price, its fills by the queue rule, the limits, the pulls, the end before the start,
and the figures with counts only in the log."""

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import polars as pl
import pytest
import quotes_fixtures as qf

from nhl_edge.live import close_forecast as cf
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
    # #376: the away side of 0.55 is 0.44999999999999996 in floats. Rounded first, it quotes
    # its cent at a cent's half-spread; floored as before ROUNDED_FROM, a cent lower.
    assert kq._quote_price(1 - 0.55, cent, None) == Decimal("0.44")
    assert kq._quote_price(1 - 0.55, cent, None, rounded=False) == Decimal("0.43")
    assert kq._quote_price(0.4449999, cent, None) == Decimal("0.43")


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


def forecasts(
    p_close_hat: float = 0.53, day: date = qf.DAY, version: str = cf.VERSION
) -> pl.DataFrame:
    """The day's close forecast for the game (ADR 0054), published before the first top."""
    return pl.DataFrame(
        {
            "game_date": [day],
            "game_id": [qf.GAME],
            "status": ["predicted"],
            "p_close_hat": [p_close_hat],
            "published_utc": [qf.PUBLISHED - timedelta(minutes=5)],
            "forecast_version": [version],
        }
    ).with_columns(pl.col("published_utc").cast(qf.UTC_TYPE))


def test_the_forecast_quotes_from_its_published_close_forecast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kq, "FORECAST_FIRST_DAY", qf.DAY)
    result = day(forecasts=forecasts())
    home = of(result, "forecast-2c").filter(pl.col("team") == qf.HOME)
    assert home["fair_at_post"].to_list()[0] == pytest.approx(0.53)
    assert home["price"].to_list() == [0.51]
    # The model's arm is unchanged by it, and before its first day the forecast quotes nothing.
    assert of(result, "model-2c").equals(of(day(), "model-2c"))
    monkeypatch.setattr(kq, "FORECAST_FIRST_DAY", qf.DAY + timedelta(days=1))
    assert of(day(forecasts=forecasts()), "forecast-2c").is_empty()
    assert result.counts["forecast-2c"]["posted"] > 0


def test_each_close_forecast_fair_value_reads_its_own_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kq, "FORECAST_FIRST_DAY", qf.DAY)
    monkeypatch.setattr(kq, "FORECAST2_FIRST_DAY", qf.DAY)
    both = pl.concat([forecasts(0.53), forecasts(0.57, version=cf.VERSION_2)])
    result = day(forecasts=both)
    first = of(result, "forecast-2c").filter(pl.col("team") == qf.HOME)
    second = of(result, "forecast2-2c").filter(pl.col("team") == qf.HOME)
    assert first["fair_at_post"].to_list()[0] == pytest.approx(0.53)
    assert second["fair_at_post"].to_list()[0] == pytest.approx(0.57)
    # Version 2's rows leave version 1's arm as it was alone (ADR 0056).
    assert first.equals(
        of(day(forecasts=forecasts(0.53)), "forecast-2c").filter(pl.col("team") == qf.HOME)
    )
    # Before its first day, version 2's arm quotes nothing.
    monkeypatch.setattr(kq, "FORECAST2_FIRST_DAY", qf.DAY + timedelta(days=1))
    assert of(day(forecasts=both), "forecast2-2c").is_empty()


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


def test_the_inventory_is_measured_after_each_fill_in_time_order() -> None:
    # 20 away contracts unmatched, then within one step an away fill of 10 and a home fill of 10:
    # the net reaches 30 between them.
    rows = [(m, qf.AWAY, 0.43, 10.0) for m in (3, 6, 10.2)] + [(10.5, qf.HOME, 0.51, 15.0)]
    result = day(trades=qf.trades(rows))
    zero = of(result, "model-0c")
    assert zero["contracts"].sum() == 40.0
    assert result.counts["model-0c"]["largest_inventory"] == 30.0


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


def test_a_per_contract_figure_weighs_each_fill_by_its_contracts() -> None:
    def frame(sizes: list[tuple[float, float]]) -> pl.DataFrame:
        days = [qf.DAY + timedelta(weeks=w) for w in range(4)]
        return pl.DataFrame(
            {
                "season": [20262027] * 4 * len(sizes),
                "game_date": [d for d in days for _ in sizes],
                "contracts": [c for _ in days for c, _ in sizes],
                "profit": [v for _ in days for _, v in sizes],
            }
        )

    whole = kq.per_contract_estimate(frame([(10.0, 0.02), (1.0, -0.10)]), "profit")
    split = kq.per_contract_estimate(frame([(5.0, 0.02), (5.0, 0.02), (1.0, -0.10)]), "profit")
    assert whole["mean"] == pytest.approx((10 * 0.02 - 0.10) / 11)
    assert (whole["mean"], whole["low"], whole["high"]) == pytest.approx(
        (split["mean"], split["low"], split["high"])
    )


def test_a_day_kept_before_its_results_gets_them_at_a_later_report(tmp_path: object) -> None:
    from pathlib import Path

    from nhl_edge.live import bundle as lb

    store = lb.LocalStore(Path(str(tmp_path)))
    early = day(games=qf.games().clear())
    assert early.fills["won"].is_null().all()
    kq.keep(store, qf.DAY, early, "kalshi-quotes-20261103-abc1234", NOW)
    found, metas = kq.kept(store)
    assert found["won"].is_null().all()
    assert kq.summary(found, metas, qf.games().clear())["model-2c"]["profit"] is None
    later = kq.summary(found, metas, qf.games())["model-2c"]
    assert later["profit"] == {"games": 1, "weeks": 1} and later["profit_usd"] > 0


def test_the_summary_and_the_log_line(tmp_path: object) -> None:
    from pathlib import Path

    from nhl_edge.live import bundle as lb

    store = lb.LocalStore(Path(str(tmp_path)))
    result = day()
    kq.keep(store, qf.DAY, result, "kalshi-quotes-20261103-abc1234", NOW)
    found, metas = kq.kept(store)
    figures = kq.summary(found, metas, qf.games())
    two = figures["model-2c"]
    assert two["fills"] == 1 and two["contracts"] == 10.0 and two["matched_contracts"] == 0.0
    # Under 4 weeks a measure has counts only.
    assert two["profit"] == {"games": 1, "weeks": 1}
    # Without Pinnacle's pair: the model and anchored arms each fill at every half-spread.
    line = kq.count_line(metas, 1)
    assert (
        line == "kalshi quotes: 1 days computed now, 1 kept (1 with watch logs), 6 fills over "
        "15 variants"
    )
    assert kq.due(qf.DAY + timedelta(days=2), kq.stored_days(store)) == [
        d for d in kq.due(qf.DAY + timedelta(days=2), []) if d != qf.DAY
    ]


# The hold rules after a move (#385), on the model only.

MOVED = {7: (0.54, 0.58)}


def held_day(**changes: object) -> kq.DayResult:
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
    return kq.run_day(qf.DAY, rules=DEMO_EXCHANGE, now=NOW, holds=True, **args)  # type: ignore[arg-type]


def fill_times(result: kq.DayResult, variant: str) -> list[datetime]:
    return of(result, variant).filter(pl.col("team") == qf.HOME)["fill_utc"].to_list()


def test_the_hold_rules_quote_the_model_alike_without_a_move() -> None:
    result = held_day()
    names = [kq.hold_name(h, hold) for h, hold in kq.hold_variants()]
    assert len(names) == 12 and names[:3] == ["model-0c-wait", "model-1c-wait", "model-2c-wait"]
    assert set(result.fills["variant"].unique()) == set(names)
    for h in kq.HALF_SPREADS:
        fills = {
            hold: of(result, kq.hold_name(h, hold)).drop("variant").sort(pl.all())
            for hold in kq.HOLDS
        }
        assert all(fills[hold].equals(fills[kq.WAIT]) for hold in kq.HOLDS)
    assert result.counts["slate"] == [[qf.GAME, 20262027]]
    # Wait is the simulator's own rule: the same fills as its model arm.
    assert of(day(), "model-2c").drop("variant").equals(of(result, "model-2c-wait").drop("variant"))


def test_trust_keeps_its_quote_through_a_move() -> None:
    result = held_day(tops=qf.tops(moved=MOVED))
    assert result.counts["model-2c-wait"]["pulled_move"] == 1
    assert result.counts["model-2c-trust"]["pulled_move"] == 0
    assert fill_times(result, "model-2c-wait") == []
    assert fill_times(result, "model-2c-trust") == [qf.at(10)]


def test_cool_quotes_again_fifteen_minutes_after_the_pull() -> None:
    trades = qf.trades([(21.5, qf.HOME, 0.51, 20.0), (23, qf.HOME, 0.51, 20.0)])
    result = held_day(tops=qf.tops(moved=MOVED), trades=trades)
    # Pulled at minute 7, held to minute 22, filled by the trade at 23.
    assert fill_times(result, "model-2c-cool") == [qf.at(23)]
    assert fill_times(result, "model-2c-wait") == []
    # The mid stays moved, so return keeps holding.
    assert fill_times(result, "model-2c-return") == []
    assert fill_times(result, "model-2c-trust") == [qf.at(21.5), qf.at(23)]


def test_return_quotes_again_when_the_moved_mid_comes_back() -> None:
    tops = qf.tops(moved={7: (0.54, 0.58), 12: (0.50, 0.54)})
    trades = qf.trades([(11.5, qf.HOME, 0.51, 20.0), (13, qf.HOME, 0.51, 20.0)])
    result = held_day(tops=tops, trades=trades)
    assert fill_times(result, "model-2c-return") == [qf.at(13)]
    assert fill_times(result, "model-2c-cool") == []
    assert fill_times(result, "model-2c-wait") == []


def test_a_starter_change_holds_under_every_rule() -> None:
    polls = [(qf.at(-60), qf.HOME, "Logan Thompson"), (qf.at(5), qf.HOME, "Charlie Lindgren")]
    trades = qf.trades([(10, qf.HOME, 0.51, 20.0), (30, qf.HOME, 0.51, 20.0)])
    result = held_day(dailyfaceoff=qf.dailyfaceoff(polls), trades=trades)
    for hold in kq.HOLDS:
        name = kq.hold_name(Decimal("0.02"), hold)
        assert result.counts[name]["pulled_starter"] == 1
        assert of(result, name).is_empty()


def test_the_minutes_quoted_in_all_and_in_the_last_three_hours() -> None:
    result = held_day(tops=qf.tops(moved=MOVED), trades=qf.trades())
    # Trust rests both markets from the first top to the stop, 10 minutes before the start.
    whole = (qf.START - kq.STOP_BEFORE - qf.at(0)).total_seconds() / 60
    trust = result.counts["model-2c-trust"]
    assert trust["quoted_minutes"] == pytest.approx(2 * whole, abs=1e-3)
    assert trust["late_minutes"] == pytest.approx(2 * 180, abs=1e-3)
    # Wait pulls both at minute 7 and never quotes again.
    wait = result.counts["model-2c-wait"]
    assert wait["quoted_minutes"] == pytest.approx(2 * 7, abs=1e-3)
    assert wait["late_minutes"] == 0


def test_the_simulators_own_record_is_unchanged() -> None:
    counts = day().counts
    assert "slate" not in counts
    assert set(counts["model-2c"]) == {
        "posted",
        "pulled_starter",
        "pulled_move",
        "fills",
        "largest_inventory",
    }


def test_the_hold_rules_record_summary_and_log_line(tmp_path: object) -> None:
    from pathlib import Path

    from nhl_edge.live import bundle as lb

    store = lb.LocalStore(Path(str(tmp_path)))
    kq.keep(store, qf.DAY, held_day(), "kalshi-quotes-20261103-abc1234", NOW, kq.HOLDS_PREFIX)
    assert kq.stored_days(store) == [] and kq.stored_days(store, kq.HOLDS_PREFIX) == [qf.DAY]
    found, metas = kq.kept(store, kq.HOLDS_PREFIX)
    assert metas[0]["version"] == "holds/1"
    figures = kq.summary(found, metas, qf.games(), holds=True)
    assert figures["version"] == "holds/1"
    assert figures["model-1c-trust"]["fills"] == 1 and "quoted_minutes" in figures["model-1c-trust"]
    # Under 4 weeks the paired measure has counts only, and no Holm.
    against = figures["against_wait"]
    assert against["model-1c-trust"] == {"games": 1, "weeks": 1} and "holm" not in against
    assert kq.holds_count_line(metas, 1) == (
        "kalshi quote holds: 1 days computed now, 1 kept, 12 fills over 12 variants"
    )


STARTER_POLLS = [(qf.at(-60), qf.HOME, "Logan Thompson"), (qf.at(5), qf.HOME, "Charlie Lindgren")]


def requoted_day(values: kq.Sources, **changes: object) -> kq.DayResult:
    """The requote record's day (ADR 0059), with values as the day's requoted ones."""
    args = {
        "ledger": qf.ledger(),
        "morning": qf.morning(),
        "odds": qf.odds(),
        "depth": qf.depth(),
        "trades": qf.trades([(10, qf.HOME, 0.51, 20.0), (30, qf.HOME, 0.51, 20.0)]),
        "tops": qf.tops(),
        "dailyfaceoff": qf.dailyfaceoff(STARTER_POLLS),
        "games": qf.games(),
    } | changes
    counted = {"changes": 1, "requoted": sum(len(v) for v in values.values())}
    return kq.run_day(
        qf.DAY,
        rules=DEMO_EXCHANGE,
        now=NOW,
        requotes=lambda inputs: (values, counted),
        **args,  # type: ignore[arg-type]
    )


def test_after_a_starter_change_requote_quotes_again_where_wait_holds() -> None:
    # The change at minute 5 pulls both; the requoted value a minute later brings requote back.
    result = requoted_day({qf.GAME: [(qf.at(6), 0.56)]})
    names = [kq.hold_name(h, rule) for h, rule in kq.requote_variants()]
    assert names[:3] == ["model-0c-wait", "model-1c-wait", "model-2c-wait"]
    assert names[3:] == ["model-0c-requote", "model-1c-requote", "model-2c-requote"]
    for rule in kq.REQUOTE_RULES:
        assert result.counts[f"model-2c-{rule}"]["pulled_starter"] == 1
    assert fill_times(result, "model-2c-wait") == []
    assert fill_times(result, "model-2c-requote")[0] == qf.at(10)
    # Requote's bid is at the requoted value: 56 less 2 cents, and a cent below the best ask.
    requoted = of(result, "model-2c-requote").filter(pl.col("team") == qf.HOME)
    assert requoted["fair_at_post"][0] == pytest.approx(0.56)
    assert requoted["price"][0] == pytest.approx(0.53)
    assert result.counts["requote"] == {"changes": 1, "requoted": 1}
    assert result.counts["slate"] == [[qf.GAME, 20262027]]
    # Wait is the simulator's own rule: the same fills as its model arm.
    own = day(
        dailyfaceoff=qf.dailyfaceoff(STARTER_POLLS),
        trades=qf.trades([(10, qf.HOME, 0.51, 20.0), (30, qf.HOME, 0.51, 20.0)]),
    )
    assert of(own, "model-2c").drop("variant").equals(of(result, "model-2c-wait").drop("variant"))


def test_a_requoted_value_quotes_only_from_its_moment() -> None:
    # Requoted at minute 12: the trade at 10 finds no bid, the one at 30 fills it.
    result = requoted_day({qf.GAME: [(qf.at(12), 0.56)]})
    assert fill_times(result, "model-2c-requote") == [qf.at(30)]


def test_without_a_requoted_value_requote_holds_as_wait() -> None:
    result = requoted_day({})
    for h in kq.HALF_SPREADS:
        wait = of(result, kq.hold_name(h, kq.WAIT)).drop("variant")
        assert of(result, kq.hold_name(h, kq.REQUOTE)).drop("variant").equals(wait)
    assert fill_times(result, "model-2c-requote") == []


def test_the_requote_record_summary_and_log_line(tmp_path: object) -> None:
    from pathlib import Path

    from nhl_edge.live import bundle as lb

    store = lb.LocalStore(Path(str(tmp_path)))
    result = requoted_day({qf.GAME: [(qf.at(6), 0.56)]})
    kq.keep(store, qf.DAY, result, "kalshi-quotes-20261108-abc1234", NOW, kq.REQUOTE_PREFIX)
    assert kq.stored_days(store, kq.HOLDS_PREFIX) == []
    assert kq.stored_days(store, kq.REQUOTE_PREFIX) == [qf.DAY]
    found, metas = kq.kept(store, kq.REQUOTE_PREFIX)
    figures = kq.summary(found, metas, qf.games(), requote=True)
    assert figures["version"] == "requote/1"
    assert figures["changes"] == {"changes": 1, "requoted": 1}
    assert "quoted_minutes" in figures["model-1c-requote"]
    # One rule beside wait: counts only under 4 weeks, and never Holm.
    against = figures["against_wait"]
    assert set(against) == {"model-0c-requote", "model-1c-requote", "model-2c-requote"}
    assert against["model-1c-requote"] == {"games": 1, "weeks": 1}
    fills = sum(figures[kq.hold_name(h, r)]["fills"] for h, r in kq.requote_variants())
    assert kq.requote_count_line(metas, 1) == (
        f"kalshi quote requotes: 1 days computed now, 1 kept, 1 starter changes, 1 requoted, "
        f"{fills} fills over 6 variants"
    )
    # It is fixed before its first scored day.
    assert kq.REQUOTE_FIXED + timedelta(days=1) == kq.REQUOTE_FIRST_DAY


def test_against_wait_pairs_each_game_and_adjusts_over_the_three_rules() -> None:
    days = [qf.DAY + timedelta(weeks=w) for w in range(5)]
    ids = [qf.GAME + w for w in range(5)]
    metas = [
        {"day": d.isoformat(), "counts": {"slate": [[g, 20262027]]}}
        for d, g in zip(days, ids, strict=True)
    ]
    # Trust gains a dollar a game over wait; cool and return match it; one game has no result.
    rules = ["model-1c-trust", "model-1c-wait", "model-1c-cool", "model-1c-return"]
    rows = pl.DataFrame(
        {
            "variant": [name for name in rules for _ in ids],
            "game_id": ids * 4,
            "profit": [0.2] * 5 + [0.1] * 15,
            "contracts": [10.0] * 20,
        }
    )
    games = pl.concat([qf.games().with_columns(game_id=pl.lit(g)) for g in ids[:4]])
    against = kq.against_wait(rows, metas, games)
    trust = against["model-1c-trust"]
    assert trust["mean"] == pytest.approx(1.0) and trust["games"] == 4
    assert against["model-1c-cool"]["mean"] == pytest.approx(0.0)
    holm = against["holm"]
    assert set(holm) == {"model-1c-cool", "model-1c-return", "model-1c-trust"}
    assert holm["model-1c-trust"]["passes"] and not holm["model-1c-cool"]["passes"]


def test_a_bid_filled_in_full_counts_as_quoted_only_until_its_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # At a net cap of 10 the home bid fills in full at minute 10.5, between two steps, and is
    # never posted again: it counts as quoted until its fill, not until the next step.
    monkeypatch.setattr(kq, "NET_CAP", Decimal(10))
    result = held_day(tops=qf.tops(minutes=21), trades=qf.trades([(10.5, qf.HOME, 0.51, 20.0)]))
    trust = result.counts["model-2c-trust"]
    whole = (qf.START - kq.STOP_BEFORE - qf.at(0)).total_seconds() / 60
    assert trust["fills"] == 1
    assert trust["quoted_minutes"] == pytest.approx(whole + 10.5, abs=1e-3)


def test_a_game_from_the_rounding_fix_quotes_the_away_side_at_its_cent() -> None:
    # The away fair value of a 0.55 home blend: before ROUNDED_FROM the bid is 0.43 and misses
    # the trade at 0.44; from it, the bid is 0.44 and fills.
    trades = qf.trades([(10, qf.AWAY, 0.44, 20.0)])
    before = of(day(trades=trades), "model-1c").filter(pl.col("team") == qf.AWAY)
    assert before.is_empty()
    dated = qf.ledger().with_columns(game_date=pl.lit(kq.ROUNDED_FROM))
    after = of(day(trades=trades, ledger=dated), "model-1c").filter(pl.col("team") == qf.AWAY)
    assert after["price"].to_list() == [0.44]


# The dashboard's figures (#398): sums per kept day and variant, never a fill's own figures.

RAW = {"price", "ticker", "team", "game_id", "posted_utc", "fill_utc", "trade_id", "order_id"}


def kept_day(tmp_path: object, result: kq.DayResult, prefix: str = kq.PREFIX) -> tuple:
    from pathlib import Path

    from nhl_edge.live import bundle as lb

    store = lb.LocalStore(Path(str(tmp_path)))
    kq.keep(store, qf.DAY, result, "kalshi-quotes-20261103-abc1234", NOW, prefix)
    return kq.kept(store, prefix)


def test_the_day_figures_sum_each_variants_fills(tmp_path: object) -> None:
    found, metas = kept_day(tmp_path, day())
    figures = kq.day_figures(found, metas, qf.games())
    assert figures.schema == pl.Schema(kq.DAY_SCHEMA)
    # The day is before forecast2's first: every other fair value at each half-spread.
    assert qf.DAY < kq.FORECAST2_FIRST_DAY
    assert sorted(figures["variant"]) == sorted(
        kq.variant_name(f, h) for f, h in kq.variants() if f != kq.FORECAST2
    )
    assert set(figures["record"]) == {kq.RECORD} and figures["hold"].is_null().all()
    two = figures.filter(pl.col("variant") == "model-2c").row(0, named=True)
    fill = of(day(), "model-2c").row(0, named=True)
    fee = fill["cost_usd"] - 0.53 * 10
    assert (two["fills"], two["contracts"], two["half_spread_cents"]) == (1, 10.0, 2)
    assert (two["cost_usd"], two["fees_usd"]) == pytest.approx((fill["cost_usd"], fee))
    assert two["edge_fair_usd"] == pytest.approx((0.55 - 0.53) * 10 - fee)
    # No last mid before the start, and no Pinnacle pair, in the fixture.
    assert (two["last_contracts"], two["edge_last_usd"]) == (0.0, 0.0)
    assert (two["close_contracts"], two["edge_close_usd"]) == (0.0, 0.0)
    # The home team won: ten dollars back.
    assert (two["settled_contracts"], two["payout_usd"]) == (10.0, 10.0)
    assert two["profit_usd"] == pytest.approx(10.0 - fill["cost_usd"])
    assert two["posted"] == metas[0]["counts"]["model-2c"]["posted"] > 0
    assert (two["games"], two["watch_logs"], two["code_version"]) == (
        1,
        True,
        "kalshi-quotes-20261103-abc1234",
    )
    assert two["kept_utc"] == NOW
    # A variant without a fill has its row, at zero.
    none = figures.filter(pl.col("variant") == "pinnacle-1c").row(0, named=True)
    assert (none["fills"], none["contracts"], none["profit_usd"]) == (0, 0.0, 0.0)
    # The profit agrees with the summary's, over every variant.
    summary = kq.summary(found, metas, qf.games())
    for row in figures.iter_rows(named=True):
        assert row["profit_usd"] == pytest.approx(summary[row["variant"]]["profit_usd"])
        assert row["matched_contracts"] == summary[row["variant"]]["matched_contracts"]


def test_each_edge_is_net_of_the_fee_on_the_contracts_it_is_known_for() -> None:
    # Two synthetic fills of model-1c: the home side's with Kalshi's last mid and Pinnacle's
    # close after it, the away side's with neither. A cent of fee on each.
    base = {
        "variant": "model-1c",
        "fair": kq.MODEL,
        "half_spread": 0.01,
        "game_date": qf.DAY,
        "season": 20262027,
        "game_id": qf.GAME,
        "start_utc": qf.START,
        "posted_utc": qf.at(0),
        "fill_utc": qf.at(5),
        "mid_5m": None,
        "mid_30m": None,
    }
    fills = pl.DataFrame(
        [
            base
            | {
                "team": qf.HOME,
                "contracts": 10.0,
                "price": 0.50,
                "cost_usd": 5.01,
                "fair_at_post": 0.53,
                "mid_last": 0.55,
                "p_close": 0.56,
                "close_after_fill": True,
                "won": None,
            },
            base
            | {
                "team": qf.AWAY,
                "contracts": 4.0,
                "price": 0.45,
                "cost_usd": 1.81,
                "fair_at_post": 0.47,
                "mid_last": None,
                "p_close": 0.44,
                "close_after_fill": False,
                "won": None,
            },
        ],
        schema=kq.FILL_SCHEMA,
    )
    meta = {
        "day": qf.DAY.isoformat(),
        "computed_utc": NOW.isoformat(),
        "code_version": "kalshi-quotes-x",
        "counts": {"games": 1, "watch_logs": True},
    }
    figures = kq.day_figures(fills, [meta], qf.games())
    row = figures.filter(pl.col("variant") == "model-1c").row(0, named=True)
    assert (row["fills"], row["contracts"], row["matched_contracts"]) == (2, 14.0, 4.0)
    assert row["fees_usd"] == pytest.approx(0.02)
    assert row["edge_fair_usd"] == pytest.approx(0.03 * 10 + 0.02 * 4 - 0.02)
    assert row["last_contracts"] == 10.0
    assert row["edge_last_usd"] == pytest.approx(0.05 * 10 - 0.01)
    # The away fill's close came before it: only the home fill's counts.
    assert row["close_contracts"] == 10.0
    assert row["edge_close_usd"] == pytest.approx(0.06 * 10 - 0.01)
    # The home team won: its ten contracts pay, the away side's four don't.
    assert (row["settled_contracts"], row["payout_usd"]) == (14.0, 10.0)
    assert row["profit_usd"] == pytest.approx(10.0 - 6.82)
    # No count of its own in the day's meta: zeros.
    assert (row["posted"], row["pulled_move"], row["largest_inventory"]) == (0, 0, 0.0)


def test_a_days_profit_is_null_until_every_fill_is_settled(tmp_path: object) -> None:
    found, metas = kept_day(tmp_path, day(games=qf.games().clear()))
    figures = kq.day_figures(found, metas, qf.games().clear())
    filled = figures.filter(pl.col("fills") > 0)
    assert filled.height and filled["profit_usd"].is_null().all()
    assert (filled["settled_contracts"] == 0).all() and (filled["payout_usd"] == 0).all()
    assert (figures.filter(pl.col("fills") == 0)["profit_usd"] == 0).all()
    # A later night, with the result, fills it in.
    later = kq.day_figures(found, metas, qf.games())
    assert later.filter(pl.col("fills") > 0)["profit_usd"].is_not_null().all()


def test_the_hold_rules_day_figures_carry_their_minutes(tmp_path: object) -> None:
    found, metas = kept_day(tmp_path, held_day(), kq.HOLDS_PREFIX)
    figures = kq.day_figures(found, metas, qf.games(), holds=True)
    assert figures.height == len(kq.hold_variants())
    assert set(figures["record"]) == {kq.HOLDS_RECORD} and set(figures["fair"]) == {kq.MODEL}
    trust = figures.filter(pl.col("variant") == "model-1c-trust").row(0, named=True)
    assert trust["hold"] == kq.TRUST and trust["fills"] == 1
    assert trust["quoted_minutes"] == metas[0]["counts"]["model-1c-trust"]["quoted_minutes"]
    assert figures["late_minutes"].is_not_null().all()


def test_the_rows_sent_are_the_migrations_columns() -> None:
    import re
    from pathlib import Path

    migration = Path("supabase/migrations/20261106120000_kalshi_quotes.sql").read_text()

    def columns(table: str) -> list[str]:
        body = re.search(rf"create table public\.{table} \((.*?)\n\);", migration, re.S)
        assert body is not None
        found = [m.group(1) for m in re.finditer(r"^  ([a-z_0-9]+) [a-z]", body.group(1), re.M)]
        return [c for c in found if c != "primary"]

    # The database sets written_utc.
    assert columns("quote_days") == [*kq.DAY_SCHEMA, "written_utc"]
    record = kq.report_record({"code_version": "x"}, qf.DAY)
    assert columns("quote_reports") == [*record, "written_utc"]
    for table, key in (("quote_days", kq.DAY_KEY), ("quote_reports", kq.REPORT_KEY)):
        assert f"create policy {table}_owner_reads on public.{table}" in migration
        assert f"primary key ({', '.join(key)})" in migration
    for record_name in (kq.RECORD, kq.HOLDS_RECORD):
        assert f"'{record_name}'" in migration


def test_no_raw_figure_reaches_the_dashboard(tmp_path: object) -> None:
    from nhl_edge.lake.supabase import json_rows

    found, metas = kept_day(tmp_path, day())
    rows = json_rows(kq.day_figures(found, metas, qf.games()))
    assert not RAW & set(kq.DAY_SCHEMA) and not RAW & set(rows[0])
    figures = {"code_version": "kalshi-quotes-x"} | kq.summary(found, metas, qf.games())
    record = kq.report_record(figures | {"odd": float("nan")}, qf.DAY + timedelta(days=1))
    assert record["figures"]["odd"] is None and record["record"] == kq.RECORD
    assert record["as_of"] == "2026-11-04" and record["code_version"] == "kalshi-quotes-x"
    assert not RAW & set(record["figures"]["model-2c"])
    assert kq.report_record(figures, qf.DAY, holds=True)["record"] == kq.HOLDS_RECORD


def test_nhl_real_quotes_copies_both_records_to_supabase(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path
    from typing import Any

    from typer.testing import CliRunner

    from nhl_edge import cli
    from nhl_edge.cli import app
    from nhl_edge.lake import supabase as sb
    from nhl_edge.live import bundle as lb

    store = lb.LocalStore(Path(str(tmp_path)))
    kq.keep(store, qf.DAY, day(), "kalshi-quotes-20261103-abc1234", NOW)
    kq.keep(store, qf.DAY, held_day(), "kalshi-quotes-20261103-abc1234", NOW, kq.HOLDS_PREFIX)

    class FakeLake:
        def pull(self, table: str) -> None:
            pass

        def read(self, table: str) -> pl.DataFrame:
            return qf.games() if table == "games" else pl.DataFrame()

    runs = iter(range(10))

    class Later(datetime):
        # Each run a minute after the last: a night's report is written once.
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return NOW + timedelta(minutes=next(runs))

    sent: list[tuple[str, Any, tuple[str, ...]]] = []

    class Fake:
        def upsert(self, table: str, frame: pl.DataFrame, key: Any) -> int:
            sent.append((table, frame, tuple(key)))
            return frame.height

        def upsert_records(self, table: str, records: Any, key: Any) -> int:
            sent.append((table, records, tuple(key)))
            return len(records)

    monkeypatch.setattr(cli, "_real_record", lambda r2: (FakeLake(), store))
    monkeypatch.setattr(cli, "datetime", Later)
    monkeypatch.setattr(kq, "due", lambda today, done, first=kq.FIRST_DAY: [])
    monkeypatch.setattr("nhl_edge.backtest.reports.version", lambda name, now: "kalshi-quotes-x")
    monkeypatch.setattr(sb.Supabase, "from_env", classmethod(lambda cls: Fake()))
    runner = CliRunner()
    # The copy is of the record in R2.
    assert runner.invoke(app, ["real", "quotes", "--supabase"]).exit_code == 2
    assert not sent
    result = runner.invoke(app, ["real", "quotes", "--r2", "--supabase"])
    assert result.exit_code == 0, result.output
    assert [(table, key) for table, _, key in sent] == [
        ("quote_days", kq.DAY_KEY),
        ("quote_reports", kq.REPORT_KEY),
    ] * 2
    days, holds = sent[0][1], sent[2][1]
    assert set(days["record"]) == {kq.RECORD} and set(holds["record"]) == {kq.HOLDS_RECORD}
    (report,) = sent[1][1]
    assert report["as_of"] == "2026-11-04" and report["record"] == kq.RECORD
    # Counts only in the log (#280).
    assert f"supabase {kq.RECORD}: {days.height} day rows and tonight's report upserted" in (
        result.output
    )
    assert "$" not in result.output and "usd" not in result.output.lower()
    # Without --supabase, nothing is sent.
    sent.clear()
    assert runner.invoke(app, ["real", "quotes", "--r2"]).exit_code == 0 and not sent


def test_nhl_real_quotes_keeps_the_requote_record_from_the_days_publications(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR 0059: the nightly keeps the requote record under its own prefix, from the day's ledger
    # and morning view with their bundles (none stored here), prints counts only, and copies
    # nothing of it to Supabase, whose quote_days takes the other two records.
    from pathlib import Path
    from typing import Any

    from typer.testing import CliRunner

    from nhl_edge import cli
    from nhl_edge.cli import app
    from nhl_edge.lake.schemas import CloseForecasts, dtypes
    from nhl_edge.live import bundle as lb
    from nhl_edge.live import requote as rq

    store = lb.LocalStore(Path(str(tmp_path)))
    tables = {
        "paper_ledger": qf.ledger(),
        "morning_predictions": qf.morning([(0.54, qf.PUBLISHED - timedelta(hours=5))]),
        "close_forecasts": pl.DataFrame(schema=dtypes(CloseForecasts)),
        "odds_snapshots": qf.odds(),
        "kalshi_depth": qf.depth(),
        "kalshi_trades": qf.trades([(10, qf.HOME, 0.51, 20.0)]),
        "kalshi_tops": qf.tops(),
        "dailyfaceoff_goalies": qf.dailyfaceoff(STARTER_POLLS),
        "games": qf.games(),
        "pregame_goalies": pl.DataFrame({"game_date": [qf.DAY]}),
        "players": pl.DataFrame(),
        "goalie_effects": pl.DataFrame(),
    }

    class FakeLake:
        def pull(self, table: str) -> None:
            pass

        def read(self, table: str) -> pl.DataFrame:
            return tables[table]

    seen: list[Any] = []

    def fake_requotes(
        games: Any, changes: Any, publications: Any, live: Any, calls: Any, stop_before: Any
    ) -> Any:
        assert stop_before == kq.STOP_BEFORE
        seen.append((changes, publications))
        return {qf.GAME: [(qf.at(6), 0.56)]}, {"changes": 1, "requoted": 1}

    class Later(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return NOW

    monkeypatch.setattr(cli, "_real_record", lambda r2: (FakeLake(), store))
    monkeypatch.setattr(cli, "datetime", Later)
    monkeypatch.setattr(
        kq,
        "due",
        lambda today, done, first=kq.FIRST_DAY: [qf.DAY] if first == kq.REQUOTE_FIRST_DAY else [],
    )
    monkeypatch.setattr(rq, "requotes", fake_requotes)
    monkeypatch.setattr("nhl_edge.backtest.reports.version", lambda name, now: "kalshi-quotes-x")
    result = CliRunner().invoke(app, ["real", "quotes", "--r2"])
    assert result.exit_code == 0, result.output
    ((changes, publications),) = seen
    assert changes == {qf.GAME: [qf.at(5)]}
    # The morning view's and the decision's, in that order, each without a bundle stored here.
    assert [p.published_utc for p in publications] == [
        qf.PUBLISHED - timedelta(hours=5),
        qf.PUBLISHED,
    ]
    assert all(p.bundle is None for p in publications)
    assert kq.stored_days(store, kq.REQUOTE_PREFIX) == [qf.DAY]
    assert kq.stored_days(store) == [] and kq.stored_days(store, kq.HOLDS_PREFIX) == []
    assert (
        "kalshi quote requotes: 1 days computed now, 1 kept, 1 starter changes, 1 requoted"
        in result.output
    )
    assert "$" not in result.output and "0.56" not in result.output
