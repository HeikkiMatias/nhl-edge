"""E3 on history (docs/plan.md §1, §5, #143): whether the prices E2's bets took look favourable
against the close, and which inputs drove each bet (§10, edge attribution).

**Closing line value.** For each bet, CLV = o_taken·p_close - 1, with o_taken the SBR opener's
price of the bet's side (the price taken) and p_close the multiplicative de-vigged probability of
that side at SBR's close (market/devig.py, ADR 0008). It is averaged per bet, and weighted by
stake, each with a weekly block bootstrap interval (hard rule 7).

The fair move is the close's de-vigged probability of the bet's side over the opener's, less 1:
whether the market moved toward the bet, with both margins left out. Since the close is de-vigged,
CLV = (1 + fair move) / the opener's overround - 1: the close's margin plays no part, and the
opener's margin is what CLV must overcome (ADR 0030). SBR's close is not Pinnacle's: its book is
unknown, and from 2018-19 it differs from the opener's (#51, #65). §1's criterion against the
Pinnacle closing proxy can only be judged live (phase 5).

**Attribution** (#154). The blend moves away from the market by
    D = logit p_blend - logit p_mkt = a + (b_m - 1)·L + w·logit p_B3,
with L = logit p_mkt and w = b_x + b_u·u. B3's log-odds splits into its terms
(audit/b3_gaps.explained): its intercept, and the inputs' parts INPUTS: the skaters' share of Δĝ
(with every goalie at gamma 1), the goalies' share (the rest of Δĝ under the start mixture), the
home term h_s and the schedule terms. Those parts mostly restate what the market already prices,
so splitting D by them would set the blend's shrinkage of the market against B3's agreement with
it, and the driver would mark favourite against underdog. Instead each input part c_k is measured
against its usual level at the game's market price, m_k + s_k·L: a least-squares line of the part
on L over the fold's earlier blend-training seasons, every game's part from its own fold's fit
at its prediction time (inputs only, no result). Then
    D = [a + (b_m - 1)·L + w·(intercept + Σ_k (m_k + s_k·L))] + Σ_k w·(c_k - m_k - s_k·L),
the first part the market's (the blend's reshaping of the market price, given what B3's inputs
usually say at it) and each other the input's departure from its usual level. Each part is
signed toward the bet's side, and the largest is the bet's driver. A season without an earlier
blend-training season has no driver.
"""

from collections.abc import Mapping, Sequence
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
PARTS = ("market", "skaters", "goalies", "home_ice", "schedule")
INPUTS = PARTS[1:]


def sbr_closes(sbr_odds: pl.DataFrame) -> pl.DataFrame:
    """SBR's closing moneyline of each game (game_id, close_home, close_away), E1's prices."""
    return market_prices(sbr_odds, Experiment.E1).select(
        "game_id", close_home="home_price", close_away="away_price"
    )


def closing_value(
    settled: pl.DataFrame, closes: pl.DataFrame, on: Sequence[str] = ("game_id",)
) -> pl.DataFrame:
    """The ledger's bets with the fair closing probability of their side (p_close), their CLV,
    and the fair move of their side from the price taken's market (home_price, away_price) to the
    close (fair_move, p_close over that market's de-vigged probability, less 1), which leaves out
    the two books' margins; all null for a bet without a close. closes gives each game's closing
    pair (game_id, close_home, close_away): SBR's on history (sbr_closes), Pinnacle's closing
    proxy live (live/settle.py), keyed on the columns on."""
    # A left join: a bet without a close stays in the ledger, with no CLV.
    priced = settled.join(closes, on=list(on), how="left")
    known = priced["close_home"].is_not_null().to_numpy()
    fair = np.full(priced.height, np.nan)
    if known.any():
        pair = priced.select("close_home", "close_away").to_numpy()[known]
        fair[known] = fair_probabilities(pair)[:, 0]
    # The opener taken, de-vigged the same way (hard rule 3).
    opener = (
        fair_probabilities(priced.select("home_price", "away_price").to_numpy())[:, 0]
        if priced.height
        else np.empty(0)
    )
    return (
        priced.with_columns(
            p_close_home=pl.Series(fair, dtype=pl.Float64).fill_nan(None),
            p_open_home=pl.Series(opener, dtype=pl.Float64),
        )
        .with_columns(
            p_close=pl.when(pl.col("side") == HOME)
            .then(pl.col("p_close_home"))
            .otherwise(1 - pl.col("p_close_home"))
        )
        .with_columns(
            clv=pl.col("price") * pl.col("p_close") - 1,
            fair_move=pl.col("p_close")
            / pl.when(pl.col("side") == HOME)
            .then(pl.col("p_open_home"))
            .otherwise(1 - pl.col("p_open_home"))
            - 1,
        )
        .drop("close_home", "close_away", "p_close_home", "p_open_home")
    )


def stake_weighted(frame: pl.DataFrame) -> dict[str, float] | None:
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
        "clv_stake_weighted": stake_weighted(frame),
        "fair_move_per_bet": bootstrap(frame, "fair_move").to_dict(),
        "positive_share": float((frame["clv"] > 0).mean()),  # type: ignore[arg-type]
        "return_per_bet": bootstrap(frame, "ret").to_dict(),
    }


def b3_parts(
    rows: pl.DataFrame, b3_tables: b3.Tables, model: b3.B3Model, season: int
) -> pl.DataFrame:
    """Per game of rows (game_id, prediction_utc), B3's log-odds in parts from the season's fit
    at the prediction time: its intercept and each of INPUTS. They are at the start mixture's
    expected Δĝ, as audit/b3_gaps reads them, so they explain B3's log-odds closely but not
    exactly."""
    through = b3.through(b3_tables, season)
    inputs = b3.game_inputs(through)
    pool = through.goalie_starts.select("game_id", "team", "goalie_id", "p_start", "observed_utc")
    usable, ready = b2.known_before(
        inputs.filter(pl.col("season") == season),
        pool,
        rows.select("game_id", "prediction_utc"),
        "observed_utc",
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
    return terms.join(skaters, on="game_id").select(
        "game_id",
        "intercept",
        skaters=weight * (pl.col("skater_delta") - mean) / scale,
        goalies=weight * (pl.col("delta_g_hat") - pl.col("skater_delta")) / scale,
        home_ice="offset",
        schedule=pl.sum_horizontal(*schedule),
    )


def usual(training: pl.DataFrame) -> dict[str, tuple[float, float]]:
    """Each input part's usual level at a market price: the least-squares line m + s·L of the
    part on L (logit_mkt) over the training games."""
    x = np.column_stack([np.ones(training.height), training["logit_mkt"].to_numpy()])
    lines = {}
    for part in INPUTS:
        alpha, beta = np.linalg.lstsq(x, training[part].to_numpy(), rcond=None)[0]
        lines[part] = (float(alpha), float(beta))
    return lines


def attribution(
    bets: pl.DataFrame,
    b3_tables: b3.Tables,
    b3_fits: dict[int, b3.B3Model],
    blend_fits: dict[int, Blend],
    p_market: pl.DataFrame,
    history: pl.DataFrame,
) -> pl.DataFrame:
    """Each bet (season, game_id, prediction_utc, side) with the parts of the blend's move from
    the market (part_<name> for each of PARTS, in log-odds, signed toward the bet's side) and its
    driver, the largest. p_market holds each bet's game's logit p_mkt and u (game_id, logit_mkt,
    u). history holds every game of the experiment's blend rows (season, game_id, prediction_utc,
    logit_mkt), whose earlier seasons give each input's usual level; b3_fits holds every season's
    fit, those seasons' included."""
    frames = []
    for (season,), rows in bets.group_by("season", maintain_order=True):
        season = int(season)  # type: ignore[arg-type]
        earlier = sorted(s for s in set(history["season"].to_list()) if s < season and s in b3_fits)
        if not earlier:
            frames.append(
                rows.with_columns(pl.lit(None, pl.Float64).alias(f"part_{p}") for p in PARTS)
            )
            continue
        training = pl.concat(
            [
                b3_parts(past, b3_tables, b3_fits[s], s).join(
                    past.select("game_id", "logit_mkt"), on="game_id"
                )
                for s in earlier
                for past in [history.filter(pl.col("season") == s)]
            ]
        )
        joined = rows.join(
            b3_parts(rows, b3_tables, b3_fits[season], season), on="game_id", how="left"
        ).join(p_market, on="game_id", how="left")
        frames.append(decompose(joined, usual(training), blend_fits[season].weights))
    if not frames:
        return bets.with_columns(driver=pl.lit(None, pl.String))
    return with_driver(pl.concat(frames, how="diagonal_relaxed"))


def decompose(
    rows: pl.DataFrame,
    lines: Mapping[str, tuple[float, float]],
    weights: tuple[float, ...],
) -> pl.DataFrame:
    """The blend's move from the market in parts, for rows holding side, logit_mkt, u and B3's
    parts (intercept and each of INPUTS, from b3_parts): part_<name> for each of PARTS, in
    log-odds, signed toward the side. The market's part is the blend's reshaping of the market
    price with each input at its usual level there (lines, from usual); each input's part is its
    departure from that level, weighted as the blend weights B3 at the game's u (weights: BLEND's
    a, b_m, b_x, b_u). The live slate (#193) splits each live bet the same way."""
    a, b_m, b_x, b_u = weights
    w = pl.lit(b_x) + pl.lit(b_u) * pl.col("u")
    level = sum(
        (pl.lit(alpha) + pl.lit(beta) * pl.col("logit_mkt") for alpha, beta in lines.values()),
        pl.lit(0.0),
    )
    sign = pl.when(pl.col("side") == HOME).then(1.0).otherwise(-1.0)
    return (
        rows.with_columns(
            part_market=pl.lit(a)
            + pl.lit(b_m - 1) * pl.col("logit_mkt")
            + w * (pl.col("intercept") + level),
            **{
                f"part_{part}": w
                * (pl.col(part) - pl.lit(alpha) - pl.lit(beta) * pl.col("logit_mkt"))
                for part, (alpha, beta) in lines.items()
            },
        )
        .with_columns(*(pl.col(f"part_{p}") * sign for p in PARTS))
        .drop("intercept", *INPUTS)
    )


def with_driver(parts: pl.DataFrame) -> pl.DataFrame:
    """Each row's driver: the largest of its parts toward its side, or null without them all."""
    out = parts.with_columns(*(pl.col(f"part_{p}").cast(pl.Float64) for p in PARTS))
    biggest = pl.concat_list([pl.col(f"part_{p}") for p in PARTS]).list.arg_max()
    found = pl.all_horizontal(*(pl.col(f"part_{p}").is_not_null() for p in PARTS))
    return out.with_columns(
        driver=pl.when(found).then(pl.lit(list(PARTS)).list.get(biggest)).otherwise(None)
    )


def market_history(every: pl.DataFrame, experiment: str) -> pl.DataFrame:
    """Every game of the experiment's blend rows (season, game_id, prediction_utc, logit_mkt):
    the earlier seasons' give each input part's usual level at a market price."""
    rows = every.filter(pl.col("experiment") == experiment)
    return rows.select(
        "season",
        "game_id",
        "prediction_utc",
        logit_mkt=pl.Series(logit(rows["p_mkt"].to_numpy()), dtype=pl.Float64),
    )


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
    # Bets without a close have no CLV, and are counted apart.
    unvalued = valued.filter(pl.col("clv").is_null()).height
    valued = valued.filter(pl.col("clv").is_not_null())
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
            for (driver,), rows in valued.filter(pl.col("driver").is_not_null())
            .sort("driver")
            .group_by("driver", maintain_order=True)
        }
        if "driver" in valued.columns
        else {}
    )
    return {
        "closing_proxy": "SBR's close, de-vigged multiplicatively: not Pinnacle's, and from "
        "2018-19 another book than the opener's (#65). CLV = (1 + fair move) / the opener's "
        "overround - 1",
        "bets_without_a_close": unvalued,
        "pooled": _summary(valued),
        "per_season": {
            str(season): _summary(rows)
            for (season,), rows in valued.sort("season").group_by("season", maintain_order=True)
        },
        "groups": {name: _summary(rows) for name, rows in by_group.items()},
        "drivers": drivers,
    }


def without_suspects(
    valued: pl.DataFrame, suspects: pl.Series, groups: pl.DataFrame
) -> dict[str, Any]:
    """E3 pooled without the bets on suspect openers (#56's list, game_id): a sensitivity, never
    the policy. The list reads the close, so E2 can't refuse those openers (ADR 0007), but one such
    price can flatter E3. Counts the bets left out of E3's pooled figures: a listed bet without a
    close was never in them."""
    listed = pl.col("game_id").is_in(suspects.implode())
    return {
        "bets_left_out": valued.filter(listed, pl.col("clv").is_not_null()).height,
        "pooled": report(valued.filter(~listed), groups)["pooled"],
    }
