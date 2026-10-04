"""The policy's bets on history (#141, ADR 0028): E2's blend (BLEND) at the SBR opener, the price
available at the prediction time (ADR 0006), selected on expected return (betting.selection),
staked and settled on a paper bankroll per season (betting.staking).

E1 is the information test at the close, not a tradable price, so it places no bets. Live bets are
decided at the 12:45 ET snapshot (ADR 0028); history has only the opener and the close, so the
opener stands in for the price taken.

Every figure has a weekly block bootstrap interval where it is a mean over bets (hard rule 7).
ROI and drawdown are secondary: a few hundred bets are mostly noise (plan §11).
"""

from pathlib import Path
from typing import Any

import polars as pl

from nhl_edge.backtest.blend import BLEND, Scales
from nhl_edge.backtest.market import Experiment, market_prices
from nhl_edge.backtest.metrics import bootstrap
from nhl_edge.backtest.walk_forward import outcomes
from nhl_edge.betting import selection, staking
from nhl_edge.betting.selection import POLICY, Policy

EXPERIMENT = Experiment.E2.value
LEDGER_FILE = "bets.csv"


def candidates(
    predictions: pl.DataFrame, every: pl.DataFrame, scales: Scales, sbr_odds: pl.DataFrame
) -> pl.DataFrame:
    """Each game E2's blend scored: its probability, the opener's two decimal prices, u in
    standard deviations of its fold's training u, and the full-game result."""
    blended = predictions.filter(
        pl.col("experiment") == EXPERIMENT, pl.col("model") == BLEND
    ).select(
        "season", "game_id", "game_date", "prediction_utc", "p_home", "home_win", "train_cutoff"
    )
    doubts = []
    for season, scale in scales.get(EXPERIMENT, {}).items():
        rows = every.filter(pl.col("experiment") == EXPERIMENT, pl.col("season") == season)
        doubts.append(rows.select("game_id").with_columns(scale.in_sds(rows)))
    if not doubts:
        return blended.with_columns(
            home_price=pl.lit(None, pl.Float64),
            away_price=pl.lit(None, pl.Float64),
            u_sd=pl.lit(None, pl.Float64),
        ).clear()
    # The price taken is the opener the blend's market input came from: the same game and the
    # same prediction time.
    prices = market_prices(sbr_odds, Experiment.E2).select(
        "game_id", "prediction_utc", "start_utc", "home_price", "away_price"
    )
    return (
        blended.join(prices, on=["game_id", "prediction_utc"])
        .join(pl.concat(doubts), on="game_id")
        .sort("season", "game_date", "game_id")
    )


def ledger(
    predictions: pl.DataFrame,
    every: pl.DataFrame,
    scales: Scales,
    sbr_odds: pl.DataFrame,
    games: pl.DataFrame,
    policy: Policy = POLICY,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Every candidate game with the policy's pick, and the settled ledger of the picked bets,
    each with when its result became public."""
    results = outcomes(games).select("game_id", "result_utc")
    picked = selection.select(
        candidates(predictions, every, scales, sbr_odds).join(results, on="game_id"), policy
    )
    settled = staking.settle(picked.filter(pl.col("picked")), policy)
    return picked, settled.with_columns(ret=pl.col("profit") / pl.col("stake"))


def _season(picked: pl.DataFrame, settled: pl.DataFrame, policy: Policy) -> dict[str, Any]:
    staked = float(settled["stake"].sum()) if settled.height else 0.0
    profit = float(settled["profit"].sum()) if settled.height else 0.0
    return {
        "games": picked.height,
        "bets": settled.height,
        "home_bets": int((settled["side"] == selection.HOME).sum()) if settled.height else 0,
        "mean_ev": float(settled["ev"].mean()) if settled.height else None,  # type: ignore[arg-type]
        "mean_price": float(settled["price"].mean()) if settled.height else None,  # type: ignore[arg-type]
        "staked": staked,
        "profit": profit,
        "roi": profit / staked if staked else None,
        "return_per_bet": bootstrap(settled, "ret").to_dict() if settled.height else None,
        "final_bankroll": policy.bankroll + profit,
        "max_drawdown": staking.drawdown(settled, policy),
        "review_drawdown_reached": staking.drawdown(settled, policy) >= staking.REVIEW_DRAWDOWN,
    }


def report(picked: pl.DataFrame, settled: pl.DataFrame, policy: Policy = POLICY) -> dict[str, Any]:
    """The policy, and per season and pooled: games considered, bets, their mean EV and price,
    units staked and won, ROI, the mean return per unit staked with its interval, the final
    bankroll and the largest drawdown."""
    return {
        "experiment": EXPERIMENT,
        "price_taken": "the SBR opener, at E2's prediction time (ADR 0006)",
        "policy": {
            "min_ev": policy.min_ev,
            "ev_per_sd": policy.ev_per_sd,
            "kelly": policy.kelly,
            "max_bet": policy.max_bet,
            "max_day": policy.max_day,
            "bankroll": policy.bankroll,
        },
        "pooled": {
            "games": picked.height,
            "bets": settled.height,
            "return_per_bet": bootstrap(settled, "ret").to_dict() if settled.height else None,
        },
        "per_season": {
            str(season): _season(
                picked.filter(pl.col("season") == season),
                settled.filter(pl.col("season") == season),
                policy,
            )
            for season in sorted(picked["season"].unique().to_list())
        },
        "listed_in": LEDGER_FILE,
    }


def write_ledger(settled: pl.DataFrame, games: pl.DataFrame, out: Path, version: str) -> Path:
    """The ledger of every bet to out/bets.csv, each with its prediction time, the blend's
    train_cutoff and the run's version."""
    path = out / LEDGER_FILE
    settled.join(games.select("game_id", "home", "away"), on="game_id").select(
        "season",
        "game_id",
        "game_date",
        "prediction_utc",
        "train_cutoff",
        pl.lit(version).alias("version"),
        "home",
        "away",
        "side",
        "price",
        "p_side",
        "ev",
        "hurdle",
        "u_sd",
        "fraction",
        "bankroll_before",
        "stake",
        "win",
        "profit",
    ).with_columns(
        pl.col("p_side", "ev", "hurdle", "u_sd", "fraction").round(4),
        pl.col("bankroll_before", "stake", "profit").round(3),
    ).write_csv(path)
    return path
