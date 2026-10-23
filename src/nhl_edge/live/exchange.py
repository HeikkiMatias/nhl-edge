"""A real bet's buy order on an exchange (ADR 0040 and 0045, #281, #321): its two parts' limit
prices, contracts and costs, from the bet's probability, hurdle and stake table. Pure arithmetic:
no exchange is called.

Kalshi's contract pays $1 if the team wins. A buy of C contracts at q dollars costs C·q plus the
fee rate·C·q·(1 - q), rounded up to the account's precision. The taker fee is the worst case for
any fill; a post-only order can't take, so the maker fee, a quarter of it, is the most a maker part
pays. A fill at a lower price costs less. The order's effective decimal price is C / cost.

- **The contracts at q:** the stake rule's euro stake at q's effective price, from the alert's
  stake table (real_ledger.stake_tables). It is converted to USD at the deposit rate, divided by
  one contract's cost and rounded down to the contract step. There are fewer while the cost in
  euros, plus a half cent, exceeds the stake the table allows at the order's own effective
  price; the margin keeps the record's stake, rounded to the cent, within the rule. A stake that
  buys less than one step is no order.
- **The limit:** the highest whole cent at which the order, so sized, still clears the bet's
  hurdle at its effective price: p·C / cost - 1 ≥ hurdle (hard rule 4). The taker part's limit L_t
  is found at the taker fee, and the maker part's L_m at the maker fee, so L_m is at least L_t. A
  fill at or below a part's limit costs less per contract, so it clears the hurdle and the stake
  rule too.
- **The maker part after the taker part (rest):** sent once the taker part's fills are known, its
  contracts keep both parts within the maker part's own planned cost, and within the stake the
  table allows at the two parts' effective price together; the maker part alone still clears the
  hurdle (ADR 0045).
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from nhl_edge.live.real import Exchange

CENTS = 100
HALF_CENT = 0.005


def _d(value: float) -> Decimal:
    return Decimal(str(value))


def _rate(exchange: Exchange, maker: bool) -> Decimal:
    return _d(exchange.maker_fee if maker else exchange.taker_fee)


def fee(contracts: Decimal, price: Decimal, exchange: Exchange, maker: bool = False) -> Decimal:
    """The taker fee of buying contracts at a price, or the maker fee, rounded up to the
    exchange's precision."""
    precision = _d(exchange.fee_precision)
    raw = _rate(exchange, maker) * contracts * price * (1 - price)
    return (raw / precision).to_integral_value(ROUND_CEILING) * precision


def cost(contracts: Decimal, price: Decimal, exchange: Exchange, maker: bool = False) -> Decimal:
    """The most buying contracts at a price can cost, in USD: the price and the taker fee, or the
    maker fee for a post-only part."""
    return contracts * price + fee(contracts, price, exchange, maker)


def _unit(price: Decimal, exchange: Exchange, maker: bool) -> Decimal:
    return price + _rate(exchange, maker) * price * (1 - price)


@dataclass(frozen=True)
class Order:
    """A buy order: its limit price in dollars, its contracts, the most it costs in USD, its
    effective decimal price at that cost, and that cost in euros at the deposit rate."""

    price: Decimal
    contracts: Decimal
    cost: Decimal
    effective: float
    stake: float


def sized(
    price: Decimal, allowed: Callable[[float], float], exchange: Exchange, maker: bool = False
) -> Decimal:
    """The contracts the stake rule allows at a price: allowed is the euro stake the rule allows at
    a decimal price (0 below the bet's minimum price)."""
    step, rate = _d(exchange.step), _d(exchange.usd_per_eur)
    unit = _unit(price, exchange, maker)
    stake = allowed(float(1 / unit))
    if stake <= 0:
        return Decimal(0)
    contracts = (_d(stake) * rate / unit / step).to_integral_value(ROUND_FLOOR) * step
    while contracts >= step:
        total = cost(contracts, price, exchange, maker)
        if float(total / rate) + HALF_CENT <= allowed(float(contracts / total)) + 1e-12:
            return contracts
        contracts -= step
    return Decimal(0)


def order(
    p_side: float,
    hurdle: float,
    allowed: Callable[[float], float],
    exchange: Exchange,
    maker: bool = False,
) -> Order | None:
    """The bet's buy order at its limit, the highest whole cent at which it clears the hurdle at
    its effective price, at the taker fee or, for the maker part, the maker fee; None when no cent
    gives one."""
    step, rate = _d(exchange.step), _d(exchange.usd_per_eur)
    for cents in range(CENTS - 1, 0, -1):
        price = Decimal(cents) / CENTS
        contracts = sized(price, allowed, exchange, maker)
        if contracts < step:
            continue
        total = cost(contracts, price, exchange, maker)
        effective = float(contracts / total)
        if p_side * effective - 1 >= hurdle:
            return Order(price, contracts, total, effective, float(total / rate))
    return None


def rest(
    price: Decimal,
    budget: Decimal,
    prior: tuple[Decimal, Decimal],
    p_side: float,
    hurdle: float,
    allowed: Callable[[float], float],
    exchange: Exchange,
) -> Decimal:
    """The maker part's contracts at a price once the taker part filled prior, its contracts and
    their cost in USD: the most whose cost at the maker fee is within budget (the maker part's
    planned cost less the taker part's), whose cost with the taker part's, plus a half cent, is
    within the stake the rule allows at the two parts' effective price together, and whose own
    effective price clears the hurdle. 0 when none does."""
    step, rate = _d(exchange.step), _d(exchange.usd_per_eur)
    filled, spent = prior
    if budget <= 0:
        return Decimal(0)
    contracts = (budget / _unit(price, exchange, True) / step).to_integral_value(ROUND_FLOOR) * step
    while contracts >= step:
        total = cost(contracts, price, exchange, maker=True)
        together = spent + total
        if (
            total <= budget
            and p_side * float(contracts / total) - 1 >= hurdle
            and float(together / rate) + HALF_CENT
            <= allowed(float((filled + contracts) / together)) + 1e-12
        ):
            return contracts
        contracts -= step
    return Decimal(0)
