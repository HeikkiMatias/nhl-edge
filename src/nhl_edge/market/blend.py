"""The market blend (docs/plan.md §5, #140, ADR 0027): the de-vigged market's log-odds and a hockey
model's log-odds combined by a logistic regression, with the model's weight moving with the
uncertainty score u (ADR 0026):

    logit p = a + b_m·logit p_mkt + (b_x + b_u·u)·logit p_model

It is fitted by plain maximum likelihood, with no penalty and nothing tuned, on out-of-sample
predictions of earlier folds only (hard rule 6), separately for E1 (p_mkt the close) and E2 (the
opener). A fit whose kind is MARKET has only a and b_m: the market recalibrated on the same
training games, the control that shows how much of a blend's gain is the model's.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import numpy as np
from numpy.typing import ArrayLike, NDArray

from nhl_edge.market.recalibration import logit, sigmoid

ITERATIONS = 50
TOLERANCE = 1e-10


class Kind(StrEnum):
    MODEL = "model"  # a, b_m, b_x and b_u
    MARKET = "market"  # a and b_m only, the control


TERMS = {Kind.MODEL: ("a", "b_m", "b_x", "b_u"), Kind.MARKET: ("a", "b_m")}


def design(
    kind: Kind, p_mkt: ArrayLike, p_model: ArrayLike | None, u: ArrayLike | None
) -> NDArray[np.float64]:
    """The fit's columns: 1, logit p_mkt and, for a model blend, logit p_model and u·logit
    p_model."""
    market = logit(p_mkt)
    columns = [np.ones_like(market), market]
    if kind is Kind.MODEL:
        if p_model is None or u is None:
            raise ValueError("a model blend needs p_model and u")
        model = logit(p_model)
        columns += [model, np.asarray(u, dtype=np.float64) * model]
    return np.column_stack(columns)


# A column counts as independent when it adds at least this much to the span of the earlier
# ones, after each is scaled to unit length.
RANK_TOLERANCE = 1e-8


def independent(x: NDArray[np.float64]) -> list[int]:
    """The columns of x, in order, that the earlier kept columns don't already span."""
    scaled = x / np.maximum(np.linalg.norm(x, axis=0), np.finfo(float).tiny)
    kept = [0]
    for j in range(1, x.shape[1]):
        trial = scaled[:, [*kept, j]]
        if np.linalg.matrix_rank(trial, tol=RANK_TOLERANCE) == len(kept) + 1:
            kept.append(j)
    return kept


@dataclass(frozen=True)
class Blend:
    """A fitted blend: its weights, their model-based standard errors, the games it read and
    the time by which every row behind it was known."""

    kind: Kind
    weights: tuple[float, ...]
    standard_errors: tuple[float, ...]
    games: int
    train_cutoff: datetime

    def predict(
        self, p_mkt: ArrayLike, p_model: ArrayLike | None = None, u: ArrayLike | None = None
    ) -> NDArray[np.float64]:
        """The blended home probability for each game."""
        return sigmoid(design(self.kind, p_mkt, p_model, u) @ np.asarray(self.weights))

    def named(self) -> dict[str, float]:
        return dict(zip(TERMS[self.kind], self.weights, strict=True))


def fit(
    kind: Kind,
    home_win: ArrayLike,
    p_mkt: ArrayLike,
    train_cutoff: datetime,
    p_model: ArrayLike | None = None,
    u: ArrayLike | None = None,
) -> Blend:
    """Fit a blend by maximum likelihood (Newton-Raphson) on the training games' full-game
    results (1 when the home team won, OT and SO included), starting from the market as it is. A
    term whose column the earlier ones span keeps a weight of 0, with no standard error."""
    x = design(kind, p_mkt, p_model, u)
    y = np.asarray(home_win, dtype=np.float64)
    if y.ndim != 1 or x.shape[0] != y.shape[0]:
        raise ValueError("the blend needs one result per game")
    if len(np.unique(y)) < 2:
        raise ValueError("the blend needs both home wins and home losses to fit")
    # A column the earlier ones already span, such as u·logit p_model when every game has the
    # same u, holds its weight at 0 instead of making the fit singular.
    active = independent(x)
    fitted = x[:, active]
    beta = np.zeros(len(active))
    if 1 in active:
        beta[active.index(1)] = 1.0  # start from the market as it is
    for _ in range(ITERATIONS):
        p = sigmoid(fitted @ beta)
        hessian = fitted.T @ (fitted * (p * (1 - p))[:, None])
        step = np.linalg.solve(hessian, fitted.T @ (y - p))
        beta = beta + step
        if np.max(np.abs(step)) < TOLERANCE:
            break
    else:
        raise ValueError(f"the blend did not converge in {ITERATIONS} iterations")
    p = sigmoid(fitted @ beta)
    hessian = fitted.T @ (fitted * (p * (1 - p))[:, None])
    weights = np.zeros(x.shape[1])
    errors = np.full(x.shape[1], np.nan)
    weights[active] = beta
    errors[active] = np.sqrt(np.diag(np.linalg.inv(hessian)))
    return Blend(
        kind=kind,
        weights=tuple(float(b) for b in weights),
        standard_errors=tuple(float(e) for e in errors),
        games=len(y),
        train_cutoff=train_cutoff,
    )
