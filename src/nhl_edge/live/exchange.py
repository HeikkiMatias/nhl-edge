"""A real bet's buy order on an exchange (ADR 0040, #281): its limit price, contracts and cost,
from the bet's probability, hurdle and stake table. Pure arithmetic: no exchange is called.

Kalshi's contract pays $1 if the team wins. A buy of C contracts at q dollars costs C·q plus the
taker fee rate·C·q·(1 - q), rounded up to the account's precision, which is the worst case for any
fill: a maker fill pays less, and a fill at a lower price costs less. The order's effective
decimal price is C / cost.

- **The contracts at q:** the stake rule's euro stake at q's effective price, from the alert's
  stake table (real_ledger.stake_tables). It is converted to USD at the deposit rate, divided by
  one contract's cost and rounded down to the contract step. There are fewer while the cost in
  euros, plus a half cent, exceeds the stake the table allows at the order's own effective
  price; the margin keeps the record's stake, rounded to the cent, within the rule. A stake that
  buys less than one step is no order.
- **The limit L:** the highest whole cent at which the order, so sized, still clears the bet's
  hurdle at its effective price: p·C / cost - 1 ≥ hurdle (hard rule 4). A fill at or below L
  costs less per contract, so it clears the hurdle and the stake rule too.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from nhl_edge.live.real import Exchange

CENTS = 100
HALF_CENT = 0.005


def _d(value: float) -> Decimal:
    return Decimal(str(value))


def fee(contracts: Decimal, price: Decimal, exchange: Exchange) -> Decimal:
    """The taker fee of buying contracts at a price, rounded up to the exchange's precision."""
    precision = _d(exchange.fee_precision)
    raw = _d(exchange.taker_fee) * contracts * price * (1 - price)
    return (raw / precision).to_integral_value(ROUND_CEILING) * precision


def cost(contracts: Decimal, price: Decimal, exchange: Exchange) -> Decimal:
    """The most buying contracts at a price can cost, in USD: the price and the taker fee."""
    return contracts * price + fee(contracts, price, exchange)


@dataclass(frozen=True)
class Order:
    """A buy order: its limit price in dollars, its contracts, the most it costs in USD, its
    effective decimal price at that cost, and that cost in euros at the deposit rate."""

    price: Decimal
    contracts: Decimal
    cost: Decimal
    effective: float
    stake: float


def sized(price: Decimal, allowed: Callable[[float], float], exchange: Exchange) -> Decimal:
    """The contracts the stake rule allows at a price: allowed is the euro stake the rule allows at
    a decimal price (0 below the bet's minimum price)."""
    step, rate = _d(exchange.step), _d(exchange.usd_per_eur)
    unit = price + _d(exchange.taker_fee) * price * (1 - price)
    stake = allowed(float(1 / unit))
    if stake <= 0:
        return Decimal(0)
    contracts = (_d(stake) * rate / unit / step).to_integral_value(ROUND_FLOOR) * step
    while contracts >= step:
        total = cost(contracts, price, exchange)
        if float(total / rate) + HALF_CENT <= allowed(float(contracts / total)) + 1e-12:
            return contracts
        contracts -= step
    return Decimal(0)


def order(
    p_side: float, hurdle: float, allowed: Callable[[float], float], exchange: Exchange
) -> Order | None:
    """The bet's buy order at its limit, the highest whole cent at which it clears the hurdle at
    its effective price, or None when no cent gives one."""
    step, rate = _d(exchange.step), _d(exchange.usd_per_eur)
    for cents in range(CENTS - 1, 0, -1):
        price = Decimal(cents) / CENTS
        contracts = sized(price, allowed, exchange)
        if contracts < step:
            continue
        total = cost(contracts, price, exchange)
        effective = float(contracts / total)
        if p_side * effective - 1 >= hurdle:
            return Order(price, contracts, total, effective, float(total / rate))
    return None
