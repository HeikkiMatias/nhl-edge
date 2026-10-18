"""A real bet's buy order on an exchange (ADR 0040, #281): the fee, the limit, the contracts, and
the alert rows and message of an exchange venue, on the synthetic day of real_fixtures."""

import json
from collections.abc import Callable, Sequence
from decimal import Decimal

import polars as pl
import pytest
import real_fixtures as rf

from nhl_edge.live import exchange as ex
from nhl_edge.live import real
from nhl_edge.live import real_ledger as rl

RULES = real.Exchange("live", usd_per_eur=1.16)
KALSHI = real.Venue("kalshi", 0.0, 0.01, RULES)
REAL_DAY = rl.Mode(rl.REAL, rf.ALERT.date())
TABLE = [(1.99, 1.2), (2.0, 1.3), (2.05, 1.4), (2.1, 1.5)]


def allowed(table: Sequence[Sequence[float]] = TABLE) -> Callable[[float], float]:
    return lambda price: rl.stake_at(table, price)


# The fee and the cost


def test_the_fee_is_rounded_up_to_the_accounts_precision() -> None:
    # 0.07 x 2.31 x 0.77 x 0.23 = 0.02863707
    assert ex.fee(Decimal("2.31"), Decimal("0.77"), RULES) == Decimal("0.0287")
    cents = real.Exchange("live", 1.16, fee_precision=0.01)
    assert ex.fee(Decimal("2.31"), Decimal("0.77"), cents) == Decimal("0.03")
    assert ex.cost(Decimal("2.31"), Decimal("0.77"), RULES) == Decimal("1.8074")


def test_one_contracts_cost_rises_with_its_price() -> None:
    costs = [ex.cost(Decimal(1), Decimal(c) / 100, RULES) for c in range(1, 100)]
    assert costs == sorted(costs) and len(set(costs)) == len(costs)


# The limit and the contracts


@pytest.mark.parametrize("p_side", [0.36, 0.45, 0.5163520783349841, 0.6, 0.74])
@pytest.mark.parametrize("hurdle", [0.02, 0.025, 0.04])
def test_the_limit_is_the_highest_cent_that_clears_the_hurdle(p_side: float, hurdle: float) -> None:
    low = rl.min_price(p_side, hurdle, 10.0)
    table = [(round(low + i * 0.01, 2), 1.0 + 0.02 * i) for i in range(40)]
    found = ex.order(p_side, hurdle, allowed(table), RULES)
    assert found is not None
    assert p_side * found.effective - 1 >= hurdle
    assert found.effective >= low  # never below the bet's minimum price
    above = found.price + Decimal("0.01")
    contracts = ex.sized(above, allowed(table), RULES)
    if contracts > 0:
        total = ex.cost(contracts, above, RULES)
        assert p_side * float(contracts / total) - 1 < hurdle


def test_the_contracts_cost_no_more_than_the_stake_rule_allows() -> None:
    found = ex.order(0.5163520783349841, 0.025, allowed(), RULES)
    assert found is not None
    assert found.contracts == found.contracts.quantize(Decimal("0.01"))
    assert found.cost == ex.cost(found.contracts, found.price, RULES)
    assert found.stake + ex.HALF_CENT <= rl.stake_at(TABLE, found.effective) + 1e-12
    # one more step would break it
    more = found.contracts + Decimal("0.01")
    total = ex.cost(more, found.price, RULES)
    assert float(total) / RULES.usd_per_eur + ex.HALF_CENT > rl.stake_at(TABLE, float(more / total))


def test_a_fill_at_a_lower_price_still_clears_the_hurdle_and_the_rule() -> None:
    p_side, hurdle = 0.5163520783349841, 0.025
    found = ex.order(p_side, hurdle, allowed(), RULES)
    assert found is not None
    for cents in range(1, round(found.price * 100) + 1):
        price = Decimal(cents) / 100
        total = ex.cost(found.contracts, price, RULES)
        effective = float(found.contracts / total)
        assert p_side * effective - 1 >= hurdle
        assert float(total) / RULES.usd_per_eur <= rl.stake_at(TABLE, effective)


def test_more_dollars_per_euro_buy_more_contracts() -> None:
    low = ex.order(0.5163520783349841, 0.025, allowed(), RULES)
    high = ex.order(0.5163520783349841, 0.025, allowed(), real.Exchange("live", 1.30))
    assert low is not None and high is not None
    assert high.contracts > low.contracts and abs(high.stake - low.stake) < 0.02


def test_a_small_stake_buys_its_whole_contract_at_no_higher_a_limit() -> None:
    whole = real.Exchange("live", 1.16, step=1.0)
    full = ex.order(0.5163520783349841, 0.025, allowed(), whole)
    small = ex.order(0.5163520783349841, 0.025, allowed([(1.99, 0.45)]), whole)
    assert full is not None and small is not None
    assert small.contracts == 1 and small.price <= full.price < Decimal("0.49")
    assert small.stake + ex.HALF_CENT <= 0.45


def test_a_stake_that_buys_no_step_is_no_order() -> None:
    whole = real.Exchange("live", 1.16, step=1.0)
    assert ex.order(0.5163520783349841, 0.025, allowed([(1.99, 0.01)]), whole) is None
    assert ex.order(0.5163520783349841, 0.025, allowed([(1.99, 0.004)]), RULES) is None
    assert ex.order(0.5163520783349841, 0.025, allowed([]), RULES) is None


# The alert on an exchange venue


def test_an_exchange_venues_alert_carries_each_bets_order() -> None:
    rows = rf.alert_rows(REAL_DAY, venue=KALSHI)
    practice = rf.alert_rows()
    assert (rows["status"] == rl.ALERTED).all()
    # the rule's own columns are the same as on any venue
    for name in ("min_price", "max_stake", "stakes"):
        assert rows[name].to_list() == practice[name].to_list()
    assert practice["exchange"].is_null().all()
    order = json.loads(rows.filter(pl.col("game_id") == 2026020053)["exchange"].item())
    found = ex.order(0.5163520783349841, 0.025, allowed(rf.table_of(rows, 2026020053)), RULES)
    assert found is not None
    assert order == {
        "env": "live",
        "limit": f"{found.price:.2f}",
        "contracts": f"{found.contracts:.2f}",
        "max_cost_usd": f"{found.cost:.4f}",
        "stake": round(found.stake, 4),
        "effective": round(found.effective, 6),
        "usd_per_eur": 1.16,
        "expires_utc": "2026-10-07T23:25:00+00:00",
    }


def test_a_stake_that_buys_no_contract_is_skipped() -> None:
    whole = real.Venue("kalshi", 0.0, 0.01, real.Exchange("live", 1.16, step=1.0))
    rows = rf.alert_rows(REAL_DAY, bankroll=1.0, venue=whole)  # stakes of 0.01 €
    assert (rows["status"] == rl.SKIP_CONTRACTS).all()
    assert rows["exchange"].is_null().all()


def test_an_exchange_venues_message_gives_each_order_and_nothing_to_enter() -> None:
    rows = rf.alert_rows(REAL_DAY, venue=KALSHI)
    text = rl.message(rf.ALERT.date(), REAL_DAY, rows, 100.0, "https://dash")
    order = json.loads(rows.filter(pl.col("game_id") == 2026020053)["exchange"].item())
    cents = round(float(order["limit"]) * 100)
    assert (
        f"  Kalshi: buy Yes WSH at most {cents}¢, {order['contracts']} contracts, at most "
        f"${float(order['max_cost_usd']):.2f} ({order['stake']:.2f} €), resting until 02:25"
    ) in text
    assert "The orders go to Kalshi automatically" in text
    assert "Record each bet on the dashboard" not in text and "minimum price" not in text


def test_the_practice_message_is_unchanged() -> None:
    text = rl.message(rf.ALERT.date(), rl.Mode(rl.PRACTICE), rf.alert_rows(), 100.0, None)
    assert "minimum price 1.99" in text and "Kalshi" not in text
