"""E3 on history (docs/plan.md §1, §5, #143): whether the prices E2's bets took look favourable
against the close, and which inputs drove each bet (§10, edge attribution).

**Closing line value.** For each bet, CLV = o_taken·p_close - 1, with o_taken the SBR opener's
price of the bet's side (the price taken) and p_close the multiplicative de-vigged probability of
that side at SBR's close (market/devig.py, ADR 0008). It is averaged per bet, and weighted by
stake, each with a weekly block bootstrap interval (hard rule 7).

SBR's close is not Pinnacle's: its book is unknown, and from 2018-19 it comes from a lower-margin
book than its opener (#51, #65). Book differences then enter CLV, mostly pushing it down. §1's
criterion against the Pinnacle closing proxy can only be judged live (phase 5).

**Attribution.** The blend moves away from the market by
    (a + (b_m - 1)·logit p_mkt) + (b_x + b_u·u)·logit p_B3,
the first part the market's own recalibration and the second B3's, which splits further into
its log-odds terms (audit/b3_gaps.explained): the skaters' share of Δĝ (with every goalie at
gamma 1), the goalies' share (the rest of Δĝ under the start mixture), the home term h_s, the
schedule terms and B3's intercept. Each part is signed toward the bet's side, and the largest is
the bet's driver.
"""

from typing import Any

import numpy as np
import polars as pl

from nhl_edge.audit.b3_gaps import explained
from nhl_edge.backtest.market import Experiment, market_prices
from nhl_edge.backtest.metrics import bootstrap, interval, resampled_means
from nhl_edge.betting.selection import HOME
from nhl_edge.game import b2, b3
from nhl_edge.market.blend import Blend
from nhl_edge.market.devig import fair_probabilities
from nhl_edge.market.recalibration import logit

SEED = 20261004
DRAWS = 2000
# The parts of the blend's move from the market, as columns part_<name>.
PARTS = ("market", "skaters", "goalies", "home_ice", "schedule", "intercept")


def closing_value(settled: pl.DataFrame, sbr_odds: pl.DataFrame) -> pl.DataFrame:
    """The ledger's bets that have an SBR close, with the fair closing probability of their side
    (p_close) and their CLV."""
    closes = market_prices(sbr_odds, Experiment.E1).select(
        "game_id", close_home="home_price", close_away="away_price"
    )
    priced = settled.join(closes, on="game_id")
    if priced.is_empty():
        return priced.with_columns(p_close=pl.lit(None, pl.Float64), clv=pl.lit(None, pl.Float64))
    fair = fair_probabilities(priced.select("close_home", "close_away").to_numpy())[:, 0]
    return (
        priced.with_columns(p_close_home=pl.Series(fair, dtype=pl.Float64))
        .with_columns(
            p_close=pl.when(pl.col("side") == HOME)
            .then(pl.col("p_close_home"))
            .otherwise(1 - pl.col("p_close_home"))
        )
        .with_columns(clv=pl.col("price") * pl.col("p_close") - 1)
        .drop("close_home", "close_away", "p_close_home")
    )


def _weighted(frame: pl.DataFrame) -> dict[str, float] | None:
    """The stake-weighted mean CLV with its weekly block bootstrap interval: the resampled weeks'
    sum of stake·CLV over their sum of stake."""
    if frame.is_empty():
        return None
    weighted = frame.with_columns(staked_clv=pl.col("clv") * pl.col("stake"))
    numerator = resampled_means(weighted, "staked_clv", np.random.default_rng(SEED), DRAWS)
    denominator = resampled_means(weighted, "stake", np.random.default_rng(SEED), DRAWS)
    value = float(weighted["staked_clv"].sum()) / float(weighted["stake"].sum())
    return interval(value, numerator / denominator).to_dict()


def _summary(frame: pl.DataFrame) -> dict[str, Any]:
    if frame.is_empty():
        return {"bets": 0}
    return {
        "bets": frame.height,
        "clv_per_bet": bootstrap(frame, "clv").to_dict(),
        "clv_stake_weighted": _weighted(frame),
        "positive_share": float((frame["clv"] > 0).mean()),  # type: ignore[arg-type]
        "return_per_bet": bootstrap(frame, "ret").to_dict(),
    }


def attribution(
    bets: pl.DataFrame,
    b3_tables: b3.Tables,
    b3_fits: dict[int, b3.B3Model],
    blend_fits: dict[int, Blend],
    p_market: pl.DataFrame,
) -> pl.DataFrame:
    """Each bet (season, game_id, prediction_utc, side) with the parts of the blend's move from
    the market (part_<name> for each of PARTS, in log-odds, signed toward the bet's side) and its
    driver, the largest. B3's terms are at the start mixture's expected Δĝ, as audit/b3_gaps reads
    them, so they explain its log-odds closely but not exactly. B3's terms
    come from the fold's own fit at the bet's prediction time. p_market holds each game's
    de-vigged market probability (game_id, p_mkt) and the blend's u (game_id, u)."""
    frames = []
    for (season,), rows in bets.group_by("season", maintain_order=True):
        model = b3_fits[int(season)]  # type: ignore[arg-type]
        fit = blend_fits[int(season)]  # type: ignore[arg-type]
        a, b_m, b_x, b_u = fit.weights
        through = b3.through(b3_tables, int(season))  # type: ignore[arg-type]
        inputs = b3.game_inputs(through)
        pool = through.goalie_starts.select(
            "game_id", "team", "goalie_id", "p_start", "observed_utc"
        )
        moments = rows.select("game_id", "prediction_utc")
        usable, ready = b2.known_before(
            inputs.filter(pl.col("season") == season), pool, moments, "observed_utc"
        )
        _, gammas = b3.multipliers(through)
        terms = explained(model, usable, b3.scenarios(usable, ready, gammas))
        delta = b3.INPUTS.index("delta_g_hat")
        weight, mean, scale = model.weights[delta], model.means[delta], model.scales[delta]
        skaters = usable.select(
            "game_id",
            skater_delta=pl.col("home_raw") * pl.col("home_base")
            - pl.col("away_raw") * pl.col("away_base"),
        )
        schedule = [f"term_{name}" for name in b3.INPUTS if name != "delta_g_hat"]
        model_weight = pl.lit(b_x) + pl.lit(b_u) * pl.col("u")
        sign = pl.when(pl.col("side") == HOME).then(1.0).otherwise(-1.0)
        parts = (
            rows.join(terms, on="game_id")
            .join(skaters, on="game_id")
            .join(p_market, on="game_id")
            .with_columns(
                part_market=pl.lit(a) + pl.lit(b_m - 1) * pl.col("logit_mkt"),
                part_skaters=model_weight * weight * (pl.col("skater_delta") - mean) / scale,
                part_goalies=model_weight
                * weight
                * (pl.col("delta_g_hat") - pl.col("skater_delta"))
                / scale,
                part_home_ice=model_weight * pl.col("offset"),
                part_schedule=model_weight * pl.sum_horizontal(*schedule),
                part_intercept=model_weight * pl.col("intercept"),
            )
            .with_columns(*(pl.col(f"part_{p}") * sign for p in PARTS))
        )
        frames.append(parts)
    if not frames:
        return bets.with_columns(driver=pl.lit(None, pl.String))
    out = pl.concat(frames, how="diagonal_relaxed")
    biggest = pl.concat_list([pl.col(f"part_{p}") for p in PARTS]).list.arg_max()
    return out.with_columns(driver=pl.lit(list(PARTS)).list.get(biggest))


def market_inputs(every: pl.DataFrame, scales: dict[int, Any], experiment: str) -> pl.DataFrame:
    """Per game of the experiment's blend rows: logit p_mkt and u on its fold's scale."""
    frames = []
    for season, scale in scales.items():
        rows = every.filter(pl.col("experiment") == experiment, pl.col("season") == season)
        frames.append(
            rows.select(
                "game_id", logit_mkt=pl.Series(logit(rows["p_mkt"].to_numpy()))
            ).with_columns(scale.score(rows))
        )
    if not frames:
        return pl.DataFrame(schema={"game_id": pl.Int64, "logit_mkt": pl.Float64, "u": pl.Float64})
    return pl.concat(frames)


def report(valued: pl.DataFrame, groups: pl.DataFrame) -> dict[str, Any]:
    """E3 pooled and per season, by attribution group (different favourites from the market, a
    season's first 28 days) and by driver."""
    flagged = valued.join(groups, on="game_id", how="left")
    by_group = {
        "different_favourites": flagged.filter(pl.col("different_favourites")),
        "same_favourite": flagged.filter(~pl.col("different_favourites")),
        "first_28_days": flagged.filter(pl.col("early_season")),
        "after_28_days": flagged.filter(~pl.col("early_season")),
    }
    drivers = (
        {
            str(driver): _summary(rows)
            for (driver,), rows in valued.sort("driver").group_by("driver", maintain_order=True)
        }
        if "driver" in valued.columns
        else {}
    )
    return {
        "closing_proxy": "SBR's close, de-vigged multiplicatively: not Pinnacle's, and from a "
        "lower-margin book than the opener from 2018-19 on (#65), which pushes CLV down",
        "pooled": _summary(valued),
        "per_season": {
            str(season): _summary(rows)
            for (season,), rows in valued.sort("season").group_by("season", maintain_order=True)
        },
        "groups": {name: _summary(rows) for name, rows in by_group.items()},
        "drivers": drivers,
    }
