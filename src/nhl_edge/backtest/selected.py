"""The blend's calibration on the bets the policy selects (#253): a diagnostic, which tunes
nothing. A forecast can be calibrated over every game yet over-confident exactly where it
disagrees with the market enough to bet, so the same figures are read on three splits, fixed on
#253 before any reading (2026-10-19).

**Each game's side** is the side of higher expected return at the price taken
(betting.selection.select's side and p_side), and its outcome that side's win on the full game,
OT and shootout included (hard rule 2).

**The splits:**
- **selected:** the selected games (bets) against the other games scored;
- **price:** the side is the favourite when its decimal price is below the other side's at the
  same quote, and the underdog otherwise (a tie counts as an underdog);
- **u:** u below its fit's training mean (u_sd < 0) against at or above it; a game without u is
  left out of this split and counted.

**For each group:** its games and weeks; the blend's calibration intercept and slope on the side's
probability (0 and 1 when calibrated); and its log loss and Brier score against B1's on the same
games, as the mean per-game difference (blend minus B1, negative when the blend is better). Every
figure has a weekly block bootstrap interval. Log loss and Brier are primary, calibration
secondary (Gneiting and Raftery 2007).
"""

from collections.abc import Callable
from typing import Any

import polars as pl

from nhl_edge.backtest import bets
from nhl_edge.backtest.blend import Scales
from nhl_edge.backtest.market import Experiment
from nhl_edge.backtest.metrics import bootstrap, calibration, log_loss, week_of
from nhl_edge.backtest.walk_forward import B1_METHOD
from nhl_edge.betting import selection
from nhl_edge.betting.selection import HOME, POLICY

SPLITS: dict[str, tuple[str, str, str]] = {
    "selected": ("selected", "bets", "other games"),
    "price": ("favourite", "favourites", "underdogs"),
    "u": ("u_low", "u below its training mean", "u at or above it"),
}


def sides(frame: pl.DataFrame, b1: str, selected: str) -> pl.DataFrame:
    """Each game on its side: the frame needs selection.select's side, price and p_side, the two
    prices, u_sd, home_win, season and game_date, B1's home probability in column b1, and the
    boolean column selected. Adds the side's outcome (won), B1's probability of the side (p_b1),
    whether the side is the favourite at the price, and whether u is below its training mean
    (null without u)."""
    home = pl.col("side") == HOME
    return frame.with_columns(
        won=pl.when(home).then(pl.col("home_win")).otherwise(1 - pl.col("home_win")).cast(pl.Int8),
        p_b1=pl.when(home).then(pl.col(b1)).otherwise(1 - pl.col(b1)),
        favourite=pl.col("price")
        < pl.when(home).then(pl.col("away_price")).otherwise(pl.col("home_price")),
        u_low=pl.col("u_sd") < 0,
        selected=pl.col(selected).fill_null(False),
    )


def figures(frame: pl.DataFrame, enough: Callable[[pl.DataFrame], bool]) -> dict[str, Any]:
    """A group's games and weeks, and once enough says so, the blend's calibration on the side and
    its log loss and Brier score against B1's, each with its interval."""
    scored = frame.with_columns(
        log_loss=log_loss(pl.col("p_side"), pl.col("won"))
        - log_loss(pl.col("p_b1"), pl.col("won")),
        brier=(pl.col("p_side") - pl.col("won")) ** 2 - (pl.col("p_b1") - pl.col("won")) ** 2,
    )
    base = {"games": scored.height, "weeks": weeks(scored)}
    if scored.is_empty() or not enough(scored):
        return base
    result = base | {
        "log_loss_minus_b1": bootstrap(scored, "log_loss").to_dict(),
        "brier_minus_b1": bootstrap(scored, "brier").to_dict(),
    }
    if scored["won"].n_unique() < 2:
        # A group of one outcome has no calibration fit (it diverges); its other figures stand.
        return result
    fitted = calibration(scored, "p_side", "won")
    return result | {name: spread.to_dict() for name, spread in fitted.items()}


def weeks(frame: pl.DataFrame) -> int:
    """The season weeks a group's games fall in, as the weekly block bootstrap resamples them."""
    if frame.is_empty():
        return 0
    return frame.select("season", week_of(pl.col("game_date")).alias("week")).unique().height


def splits(frame: pl.DataFrame, enough: Callable[[pl.DataFrame], bool]) -> dict[str, Any]:
    """Every split's two groups, from sides' frame, with the games each split leaves out."""
    result: dict[str, Any] = {"games": frame.height}
    for name, (column, yes, no) in SPLITS.items():
        known = frame.filter(pl.col(column).is_not_null())
        result[name] = {
            yes: figures(known.filter(pl.col(column)), enough),
            no: figures(known.filter(~pl.col(column)), enough),
            "left_out": frame.height - known.height,
        }
    return result


def history(
    predictions: pl.DataFrame, every: pl.DataFrame, scales: Scales, sbr_odds: pl.DataFrame
) -> dict[str, Any]:
    """The splits on the development backtest's blends (#253), for E2 at the opener and E1 at the
    close: each game on its side at the experiment's own price, selected by the frozen policy's
    rule there. E1 places no bets (ADR 0028), so its selected games are the rule at the close, a
    diagnostic only. These seasons are seen, so it tunes nothing."""
    result: dict[str, Any] = {
        "issue": 253,
        "note": "a diagnostic on seen seasons: it tunes nothing, and any recalibration it "
        "suggests is a challenger (#250), never a change to the frozen policy",
    }
    for experiment in (Experiment.E2.value, Experiment.E1.value):
        scored = bets.candidates(predictions, every, scales, sbr_odds, experiment)
        b1 = predictions.filter(
            pl.col("experiment") == experiment,
            pl.col("model") == "B1",
            pl.col("method") == B1_METHOD,
        ).select("game_id", p_b1_home="p_home")
        picked = selection.select(scored, POLICY).join(b1, on="game_id")
        result[experiment] = splits(sides(picked, "p_b1_home", "picked"), lambda f: f.height > 1)
    return result
