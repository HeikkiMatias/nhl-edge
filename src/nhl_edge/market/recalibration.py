"""B1, the recalibrated market (docs/plan.md section 1): a logistic regression of the full-game home
win on the de-vigged market's log-odds, fitted chronologically. It corrects the market's own
biases, such as favourite-longshot or a home bias, with no hockey information, so a model that
beats B0 but not B1 is only correcting market calibration.

The fit has two parameters, p = sigmoid(intercept + slope * logit(p_market)), and no penalty.
An intercept of 0 and a slope of 1 return the market unchanged. A slope below 1 pulls favourites
toward even, and the intercept moves every game toward the home or the away side.

Every fit carries its train_cutoff, the time by which every training game's result was public. The
backtest fits one per fold, on games whose results were public before the fold's first start.
"""

from dataclasses import dataclass
from datetime import datetime

import numpy as np
from numpy.typing import ArrayLike, NDArray

# The market's probabilities are kept this far from 0 and 1, so their log-odds stay finite.
EPSILON = 1e-9
ITERATIONS = 50
TOLERANCE = 1e-12


def logit(p: ArrayLike) -> NDArray[np.float64]:
    clipped = np.clip(np.asarray(p, dtype=np.float64), EPSILON, 1 - EPSILON)
    return np.log(clipped / (1 - clipped))


def sigmoid(x: NDArray[np.float64]) -> NDArray[np.float64]:
    return 1 / (1 + np.exp(-x))


@dataclass(frozen=True)
class Recalibration:
    """A fitted B1: the market's log-odds, rescaled and shifted."""

    intercept: float
    slope: float
    games: int
    train_cutoff: datetime

    def predict(self, p_market: ArrayLike) -> NDArray[np.float64]:
        """The recalibrated home probability for each de-vigged market probability."""
        return sigmoid(self.intercept + self.slope * logit(p_market))


def fit(p_market: ArrayLike, home_win: ArrayLike, train_cutoff: datetime) -> Recalibration:
    """Fit B1 by maximum likelihood (Newton-Raphson) on the training games' de-vigged market
    probabilities and full-game results (1 when the home team won, OT and SO included)."""
    x = logit(p_market)
    y = np.asarray(home_win, dtype=np.float64)
    if x.shape != y.shape or x.ndim != 1:
        raise ValueError("p_market and home_win need one value per game")
    if len(np.unique(y)) < 2:
        raise ValueError("B1 needs both home wins and home losses to fit")
    design = np.column_stack([np.ones_like(x), x])
    beta = np.array([0.0, 1.0])  # start from the market as it is
    for _ in range(ITERATIONS):
        p = sigmoid(design @ beta)
        gradient = design.T @ (y - p)
        hessian = design.T @ (design * (p * (1 - p))[:, None])
        step = np.linalg.solve(hessian, gradient)
        beta = beta + step
        if np.max(np.abs(step)) < TOLERANCE:
            break
    else:
        raise ValueError(f"B1 did not converge in {ITERATIONS} iterations")
    return Recalibration(
        intercept=float(beta[0]), slope=float(beta[1]), games=len(y), train_cutoff=train_cutoff
    )
