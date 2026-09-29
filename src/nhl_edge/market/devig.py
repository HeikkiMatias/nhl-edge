"""De-vig: fair probabilities from one book's prices for one market. The only de-vig path (hard
rule 3): every probability taken from a price comes out of fair_probabilities.

A book's decimal prices o_i imply pi_i = 1 / o_i, which sum to the overround P, above 1 by the
book's margin. Each method takes the margin off differently (the nhl-domain skill):
- multiplicative: p_i = pi_i / P, the margin in proportion to each price.
- power: p_i = pi_i ** k, with k solving sum of p_i = 1. More of the margin comes off longshots.
- shin: p_i = (sqrt(z^2 + 4 (1 - z) pi_i^2 / P) - z) / (2 (1 - z)), with z solving sum of p_i = 1.
  Shin (1993) models a book facing a share z of insiders. With two outcomes, as on the moneyline,
  it takes the same margin off each side: p_i = pi_i - (P - 1) / 2.

Which method is the default is decided by an ADR after the phase 1 audit (#10), so every caller
names one. A row of prices is one book's prices for every outcome of one market, quoted together;
a row that mixes books or times is not a market and can sum below 1.
"""

from enum import StrEnum

import numpy as np
from numpy.typing import ArrayLike, NDArray


class Method(StrEnum):
    MULTIPLICATIVE = "multiplicative"
    POWER = "power"
    SHIN = "shin"


# Float error allowed on a zero-margin market: 2.00 and 2.00 must not read as below 1.
OVERROUND_TOLERANCE = 1e-9
# Bisection halves the bracket each step; 100 steps are far below float precision.
BISECTION_STEPS = 100


def overround(prices: ArrayLike) -> NDArray[np.float64]:
    """Sum of the implied probabilities per market: 1 plus the book's margin. Prices are decimal,
    one market per row, or a single market as a 1-D array, which gives a 0-D array."""
    table, single = _table(prices)
    total = (1.0 / table).sum(axis=1)
    return total.reshape(()) if single else total


def fair_probabilities(prices: ArrayLike, method: Method) -> NDArray[np.float64]:
    """Fair probabilities, in the order of the prices, summing to 1 per market. Prices are
    decimal, one market per row with a column per outcome, or a single market as a 1-D array.

    Raises ValueError for a price that is not above 1, fewer than two outcomes, or a market whose
    implied probabilities sum below 1, which one book's prices for one market never do."""
    table, single = _table(prices)
    implied = 1.0 / table
    total = implied.sum(axis=1)
    below = np.flatnonzero(total < 1.0 - OVERROUND_TOLERANCE)
    if below.size:
        row = int(below[0])
        raise ValueError(
            f"market {row} has implied probabilities summing to {total[row]:.6f}, below 1: "
            "not one book's prices for one market"
        )
    match method:
        case Method.MULTIPLICATIVE:
            fair = implied
        case Method.POWER:
            fair = _power(implied)
        case Method.SHIN:
            fair = _shin(implied, total)
    fair = fair / fair.sum(axis=1, keepdims=True)
    return fair[0] if single else fair


def _table(prices: ArrayLike) -> tuple[NDArray[np.float64], bool]:
    table = np.asarray(prices, dtype=np.float64)
    single = table.ndim == 1
    if single:
        table = table[np.newaxis, :]
    if table.ndim != 2:
        raise ValueError(f"prices must be one market or a table of markets, not {table.ndim}-D")
    if table.shape[1] < 2:
        raise ValueError(f"a market needs at least two outcomes, got {table.shape[1]}")
    bad = np.flatnonzero(~(np.isfinite(table) & (table > 1.0)).all(axis=1))
    if bad.size:
        row = int(bad[0])
        raise ValueError(f"market {row} has a decimal price that is not above 1: {table[row]}")
    return table, single


def _power(implied: NDArray[np.float64]) -> NDArray[np.float64]:
    # sum(pi ** k) falls as k grows. It is P >= 1 at k = 1 and at most n * max(pi) ** k, which is
    # 1 at k = log(n) / -log(max(pi)), so the root lies between the two.
    lo = np.ones(len(implied))
    hi = np.maximum(np.log(implied.shape[1]) / -np.log(implied.max(axis=1)), 1.0)
    for _ in range(BISECTION_STEPS):
        mid = (lo + hi) / 2
        above = (implied ** mid[:, np.newaxis]).sum(axis=1) > 1.0
        lo = np.where(above, mid, lo)
        hi = np.where(above, hi, mid)
    return implied ** ((lo + hi) / 2)[:, np.newaxis]


def _shin(implied: NDArray[np.float64], total: NDArray[np.float64]) -> NDArray[np.float64]:
    # The sum falls as z grows: sqrt(P) >= 1 at z = 0, and it tends to sum(pi^2) / P < 1 as z
    # tends to 1, so the root lies in [0, 1).
    scaled = implied**2 / total[:, np.newaxis]

    def probabilities(z: NDArray[np.float64]) -> NDArray[np.float64]:
        z = z[:, np.newaxis]
        return (np.sqrt(z**2 + 4 * (1 - z) * scaled) - z) / (2 * (1 - z))

    lo = np.zeros(len(implied))
    hi = np.ones(len(implied))
    for _ in range(BISECTION_STEPS):
        mid = (lo + hi) / 2
        above = probabilities(mid).sum(axis=1) > 1.0
        lo = np.where(above, mid, lo)
        hi = np.where(above, hi, mid)
    return probabilities(lo)
