"""Quarter-Kelly staking and settlement of a paper bankroll (docs/plan.md §5, §11, #141, ADR 0028).

A picked bet's stake is a share of the bankroll at the start of its day:

    f = kelly·EV / (o - 1) / (1 + u⁺)

where u⁺ is u's standard deviations above its training mean, or 0 at or below it. It is capped at
max_bet. When a day's shares add up to more than max_day, they are all scaled down together to
max_day. Every bet of a day is decided before any of its games is played, so a day's stakes read
the bankroll left by the earlier days' results only.

Bets settle on the full game, overtime and shootout included (hard rule 2): the home side wins when
home_win is 1. A won bet returns stake·(o - 1), a lost one -stake. Each season starts its own
paper bankroll. Live, a 20% drawdown calls for a review of data and code, never a model change
(§11); the backtest reports the drawdown without a verdict on it (hard rule 7).
"""

import numpy as np
import polars as pl

from nhl_edge.betting.selection import HOME, POLICY, Policy, doubt


def fractions(bets: pl.DataFrame, policy: Policy = POLICY) -> pl.DataFrame:
    """Each picked bet (ev, price, u_sd, game_date) with its share of the day's bankroll
    (fraction), the Kelly share capped per bet and scaled down to the day's cap."""
    kelly = (
        policy.kelly * pl.col("ev") / (pl.col("price") - 1) / (1 + doubt(pl.col("u_sd")))
    ).clip(0.0, policy.max_bet)
    return (
        bets.with_columns(capped=kelly)
        .with_columns(day_total=pl.col("capped").sum().over("season", "game_date"))
        .with_columns(
            fraction=pl.when(pl.col("day_total") > policy.max_day)
            .then(pl.col("capped") * policy.max_day / pl.col("day_total"))
            .otherwise(pl.col("capped"))
        )
        .drop("capped", "day_total")
    )


def won(side: pl.Expr, home_win: pl.Expr) -> pl.Expr:
    """Whether the bet's side won the full game (OT and SO included)."""
    return pl.when(side == HOME).then(home_win == 1).otherwise(home_win == 0)


def check_days(bets: pl.DataFrame) -> None:
    """Refuse a ledger whose decisions could read the future. Each bet must be decided before its
    own game starts, and a day's bets must share one decision time, so the day cap scales
    decisions made together, from what was known then (prediction_utc, start_utc). A game already
    under way at that time is never an input: the blend and the prices are fixed before it."""
    late = bets.filter(pl.col("prediction_utc") >= pl.col("start_utc")).sort("game_date")
    if late.height:
        raise ValueError(f"a bet of {late['game_date'][0]} was decided after its game started")
    days = (
        bets.group_by("season", "game_date")
        .agg(decisions=pl.col("prediction_utc").n_unique())
        .filter(pl.col("decisions") > 1)
        .sort("game_date")
    )
    if days.height:
        raise ValueError(f"bets of {days['game_date'][0]} were decided at more than one time")


def settle(bets: pl.DataFrame, policy: Policy = POLICY) -> pl.DataFrame:
    """The ledger: each picked bet (season, game_date, game_id, side, price, ev, u_sd, home_win,
    prediction_utc, start_utc and result_utc) with its fraction, its stake in units, whether it
    won, its profit, and the bankroll its day staked. A day's bankroll counts only the earlier
    bets whose results were public before the day's first decision; a later result counts from
    the first day after it became public."""
    check_days(bets)
    staked = fractions(bets, policy).sort("season", "game_date", "game_id")
    out = []
    for (_,), season in staked.group_by("season", maintain_order=True):
        days = season.with_columns(win=won(pl.col("side"), pl.col("home_win")))
        settled_results: list[tuple[object, float]] = []
        befores, stakes, profits = [], [], []
        for (_,), day in days.group_by("game_date", maintain_order=True):
            decision = day["prediction_utc"].min()
            bankroll = policy.bankroll + sum(
                profit
                for public, profit in settled_results
                if public < decision  # type: ignore[operator]
            )
            day_stakes = day["fraction"].to_numpy() * bankroll
            day_profit = np.where(
                day["win"].to_numpy(), day_stakes * (day["price"].to_numpy() - 1), -day_stakes
            )
            befores += [bankroll] * day.height
            stakes += day_stakes.tolist()
            profits += day_profit.tolist()
            settled_results += list(
                zip(day["result_utc"].to_list(), day_profit.tolist(), strict=True)
            )
        out.append(
            days.with_columns(
                bankroll_before=pl.Series(befores, dtype=pl.Float64),
                stake=pl.Series(stakes, dtype=pl.Float64),
                profit=pl.Series(profits, dtype=pl.Float64),
            )
        )
    if not out:
        return staked.with_columns(
            win=pl.lit(None, pl.Boolean),
            bankroll_before=pl.lit(None, pl.Float64),
            stake=pl.lit(None, pl.Float64),
            profit=pl.lit(None, pl.Float64),
        )
    return pl.concat(out)


def drawdown(ledger: pl.DataFrame, policy: Policy = POLICY) -> float:
    """The largest fall of the bankroll from its peak, as a share of the peak, over one season's
    ledger, with each result applied when it became public (result_utc)."""
    if ledger.is_empty():
        return 0.0
    steps = (
        ledger.group_by("result_utc").agg(pl.col("profit").sum()).sort("result_utc")["profit"]
    ).to_numpy()
    path = policy.bankroll + np.concatenate([[0.0], np.cumsum(steps)])
    peaks = np.maximum.accumulate(path)
    return float(np.max((peaks - path) / peaks))
