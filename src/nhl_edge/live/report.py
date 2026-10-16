"""The live report (#166, docs/plans/phase-5.md task 6), under ADR 0032's rules.

`nhl live report` writes reports/live/report-<date>.md and .json, committed weekly. Every figure
has its weekly block bootstrap interval (hard rule 7). A figure over fewer than MIN_WEEKS weeks of
games shows neither a value nor an interval, only its counts: a handful of weeks gives an
interval that understates the uncertainty. Until the formal review on REVIEW_DATE every report is
interim. It gives no verdict, and no promotion or real stake follows from it.

It covers the frozen policy's regular-season paper decisions (ADR 0032):
- **Coverage:** the slate games, those predicted, the bets, the eligible bets and the bets with a
  valid closing proxy, every exclusion by reason, and the quotes' freshness apart from their lead
  before the start.
- **The primary measure:** mean CLV per paper bet against Pinnacle's fair closing proxy, over the
  eligible bets with one. Stake-weighted CLV and the fair move are shown beside it, never instead
  of it. Then the coverage floor (FLOOR of the eligible bets have a proxy) and the bound: the
  mean again with each eligible bet without a proxy counted at a BOUND percentile of the observed
  CLV, imputed before the bootstrap.
- **The model comparisons,** by log loss and paired on the same games: BLEND - B1 at the decision
  price (live E2), B3 - B2 (hard rule 3), BLEND - BLEND_B2 and BLEND - BLEND_MARKET.
- **The blend's calibration band:** the probability recalibrated on the live games, at each
  forecast p in BAND_AT, stays within BAND·p of p.
- **For hand review:** the games where the blend and B1 differ by more than GAP (hard rule 8).
- **Operational alerts,** which never change the policy: days skipped and prices missing or
  stale, the guard's firing rate, u outside its training range (ADR 0030), and the DRAWDOWN review
  of data and code.
- **ADR 0030's other check:** whether Pinnacle's 12:45 price sits nearer SBR's opener or its
  close, by B0's log loss.
- **Entry to close (#251),** a diagnostic with no verdict: over every predicted game with a close,
  whether Pinnacle's fair price moved toward each model's disagreement with it at the decision
  (live/movement.py).
- **Shadow evaluations (#252),** paper only and with no verdict: the frozen rules on the
  market-only control, and the policy's own bets at the best fresh price of the other books
  (live/shadow.py).
- **Challengers (ADR 0038),** with no verdict: every registered challenger over the decision days
  after its registration (live/challengers.py), against the blend and B1 by paired log loss, its
  shadow bets under the frozen rules, and its disagreement against the move to the close.
- **Real stakes (ADR 0039, #267):** the start rule's conditions and verdict, with the paper
  bankroll that its owner's veto reads, and the weekly CLV stop (live/real.py). The frozen
  policy's report only.
- **Return at the taken price,** and the drawdown's size, only from REVIEW_DATE on (plan §11).

It reads ledgers, settlements, results and prices only: nothing here feeds a decision.
"""

import json
import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from nhl_edge.backtest import b2_report
from nhl_edge.backtest.e3 import stake_weighted
from nhl_edge.backtest.market import Experiment, market_prices
from nhl_edge.backtest.metrics import (
    bootstrap,
    calibration_draws,
    difference,
    interval,
    log_loss,
    slope,
    week_of,
)
from nhl_edge.backtest.walk_forward import B1_METHOD, b0, bounds, implausible, outcomes
from nhl_edge.betting.selection import FROZEN_ON, POLICY, POLICY_VERSION
from nhl_edge.game.uncertainty import PARTS
from nhl_edge.live import challengers as lc
from nhl_edge.live import movement, real, shadow
from nhl_edge.live import predict as lp
from nhl_edge.live import settle as ls
from nhl_edge.market import closing

# The formal review, after the last regular-season result is public (ADR 0032).
REVIEW_DATE = date(2027, 4, 12)
MIN_WEEKS = 4
FLOOR = 0.90
# The percentiles of the observed CLV that stand in for an eligible bet without a proxy: the
# lower one tests a favourable verdict, the upper one an unfavourable one.
BOUND = (10, 90)
BAND = 0.025
BAND_AT = (0.35, 0.50, 0.65)
GAP = b2_report.GAP
DRAWDOWN = 0.20
# How long a challenger's day without its output is pending, from the day's UTC midnight: the
# nightly scores it the night after its decision (live/challengers.py).
PENDING_FOR = timedelta(days=2)
# u's distance from its training mean, in its training standard deviations, that the report
# counts: under a normal spread about 4.6% and 0.3% of games lie beyond them.
U_LIMITS = (2.0, 3.0)
# SBR's seasons for ADR 0030's market check: the blend's training seasons whose results are open.
HISTORY = (20182019, 20192020, 20202021, 20212022)
REGULAR_SEASON = 2
COMPARISONS = {
    "BLEND - B1": ("p_blend", "p_b1"),
    "B3 - B2": ("p_b3", "p_b2"),
    "BLEND - BLEND_B2": ("p_blend", "p_blend_b2"),
    "BLEND - BLEND_MARKET": ("p_blend", "p_blend_market"),
}
FAVOURABLE, UNFAVOURABLE = "favourable prices", "unfavourable prices"
PASS, FAIL = "pass", "fail"
INSUFFICIENT = "insufficient evidence"
INTERIM = "interim: no verdict before the formal review"
SKIPPED = (lp.NO_SNAPSHOT, lp.LATE)
ROLES = {"F": "forwards", "D": "defencemen"}


def counted(ledger: pl.DataFrame, as_of: date, policy: str | None = POLICY_VERSION) -> pl.DataFrame:
    """The ledger rows the evidence counts: one policy's regular-season slate games up to as_of,
    the frozen policy's by default (ADR 0032; a new policy version starts its own record, and an
    earlier one's stays on record under it, ADR 0037). With policy None, every policy's: the
    season's one paper bankroll, which a new version carries on (ADR 0037)."""
    of_policy = pl.lit(True) if policy is None else pl.col("policy_version") == policy
    return ledger.filter(
        of_policy,
        (pl.col("game_id") // 10_000 % 100) == REGULAR_SEASON,
        pl.col("game_date") <= as_of,
    ).sort("game_date", "game_id")


def weeks(frame: pl.DataFrame) -> int:
    """How many season weeks the frame's games fall in, as the bootstrap resamples them."""
    if frame.is_empty():
        return 0
    return frame.select("season", week_of(pl.col("game_date")).alias("week")).unique().height


def enough(frame: pl.DataFrame) -> bool:
    return weeks(frame) >= MIN_WEEKS


def counts(frame: pl.DataFrame) -> dict[str, Any]:
    """A figure's counts alone, for one over fewer than MIN_WEEKS weeks."""
    return {"games": frame.height, "weeks": weeks(frame)}


def estimate(frame: pl.DataFrame, value: str) -> dict[str, Any]:
    """value's mean over the frame's games with its weekly block bootstrap interval, or only the
    counts when they span fewer than MIN_WEEKS weeks."""
    if not enough(frame):
        return counts(frame)
    return bootstrap(frame, value).to_dict()


def scoreable(rows: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """The predicted rows, but a decision on a game later played more than POSTPONED from the
    start it was decided for: that forecast is void, as its bet is (live/settle.py), and the game
    is decided again on its new date."""
    played = games.select("game_id", played_utc="start_utc")
    return (
        rows.filter(pl.col("status") == lp.PREDICTED)
        .join(played, on="game_id", how="left")
        .filter(
            pl.col("played_utc").is_null()
            | ((pl.col("played_utc") - pl.col("start_utc")).abs() <= ls.POSTPONED)
        )
        .drop("played_utc")
    )


def with_results(rows: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """The scoreable predicted rows whose game's result is known, with it (home_win)."""
    return scoreable(rows, games).join(outcomes(games).select("game_id", "home_win"), on="game_id")


def bets_of(rows: pl.DataFrame, settlements: pl.DataFrame) -> pl.DataFrame:
    """The counted rows' bets with their settlement, when there is one (settled is null until
    then)."""
    keys = ["game_date", "game_id"]
    settled = settlements.select(
        *keys,
        "status",
        "close_status",
        "close_snapshot_utc",
        "clv",
        "fair_move",
        "profit",
        "result_utc",
    ).rename({"status": "settled"})
    return rows.filter(pl.col("bet").fill_null(False)).join(settled, on=keys, how="left")


def coverage(rows: pl.DataFrame, bets: pl.DataFrame, games: pl.DataFrame) -> dict[str, Any]:
    """ADR 0032's denominators: the slate games, those predicted and why the others weren't, the
    slate games still without a result, the bets, and how many are eligible and have a valid
    proxy, with every exclusion by reason."""
    not_predicted = rows.filter(pl.col("status") != lp.PREDICTED)
    # Every slate game, predicted or not: the formal review waits for all their results.
    awaiting = rows.join(games.select("game_id"), on="game_id", how="anti").height
    settled = bets.filter(pl.col("settled") == ls.SETTLED)
    eligible = settled.filter(pl.col("close_status") != closing.NO_PREGAME)
    proxy = eligible.filter(pl.col("close_status") == closing.PROXY)
    return {
        "slate_games": rows.height,
        "predicted": rows.height - not_predicted.height,
        "not_predicted": _counts(not_predicted, "status"),
        "awaiting_result": awaiting,
        "bets": bets.height,
        "awaiting_settlement": bets.filter(pl.col("settled").is_null()).height,
        "void_postponed": bets.filter(pl.col("settled") == ls.VOID).height,
        "settled": settled.height,
        "no_pregame_snapshot": settled.height - eligible.height,
        "eligible": eligible.height,
        "with_proxy": proxy.height,
        "eligible_without_proxy": _counts(
            eligible.filter(pl.col("close_status") != closing.PROXY), "close_status"
        ),
        "share_with_proxy": proxy.height / eligible.height if eligible.height else None,
    }


def _counts(frame: pl.DataFrame, column: str) -> dict[str, int]:
    return {str(k): int(n) for k, n in frame.group_by(column).len().sort(column).iter_rows()}


def freshness(rows: pl.DataFrame, bets: pl.DataFrame, odds: pl.DataFrame) -> dict[str, Any]:
    """Each quote's age apart from its lead before the start, in minutes (median and largest):
    Pinnacle's decision quote at the decision, and each valid closing proxy's age at its snapshot
    and lead before the start. Data quality, not a measure of the model."""
    predicted = rows.filter(pl.col("status") == lp.PREDICTED)
    decision_age = (pl.col("prediction_utc") - pl.col("last_update_utc")).dt.total_seconds() / 60
    proxies = bets.filter(pl.col("close_status") == closing.PROXY, pl.col("settled") == ls.SETTLED)
    lead = (pl.col("start_utc") - pl.col("close_snapshot_utc")).dt.total_seconds() / 60
    pinnacle = odds.filter(pl.col("book") == closing.BOOK, pl.col("market") == "h2h").select(
        "event_id",
        close_snapshot_utc="snapshot_utc",
        age=(pl.col("snapshot_utc") - pl.col("last_update_utc")).dt.total_seconds() / 60,
    )
    ages = (
        proxies.join(pinnacle, on=["event_id", "close_snapshot_utc"])
        .group_by("game_date", "game_id")
        .agg(pl.col("age").max())
    )
    return {
        "decision_quote_age_min": _spread(predicted.select(decision_age.alias("x"))["x"]),
        "proxy_age_min": _spread(ages["age"]) if ages.height else None,
        "proxy_lead_min": _spread(proxies.select(lead.alias("x"))["x"]) if proxies.height else None,
    }


def _spread(values: pl.Series) -> dict[str, float] | None:
    values = values.drop_nulls()
    if values.is_empty():
        return None
    return {"median": float(values.median()), "max": float(values.max())}  # type: ignore[arg-type]


def closing_value(bets: pl.DataFrame) -> dict[str, Any]:
    """The primary measure over the eligible bets with a valid proxy, the stake-weighted CLV and
    the fair move beside it, the coverage floor, and the bound at each BOUND percentile."""
    eligible = bets.filter(
        pl.col("settled") == ls.SETTLED, pl.col("close_status") != closing.NO_PREGAME
    )
    valued = eligible.filter(pl.col("close_status") == closing.PROXY)
    share = valued.height / eligible.height if eligible.height else None
    result: dict[str, Any] = {
        "clv_per_bet": estimate(valued, "clv"),
        # Over fewer than MIN_WEEKS weeks, only its counts (Codex on #235).
        "clv_stake_weighted": (stake_weighted(valued) if enough(valued) else counts(valued))
        if valued.height
        else None,
        "fair_move_per_bet": estimate(valued, "fair_move"),
        "floor": {"required": FLOOR, "share_with_proxy": share},
        "bound": {},
    }
    if valued.is_empty():
        return result
    for percentile in BOUND:
        # A percentile of the observed CLV is withheld too until MIN_WEEKS weeks (Codex on #235).
        stand_in = (
            float(np.percentile(valued["clv"].to_numpy(), percentile)) if enough(valued) else None
        )
        bound = pl.col("clv") if stand_in is None else pl.col("clv").fill_null(stand_in)
        imputed = eligible.with_columns(clv_bound=bound)
        result["bound"][f"p{percentile}"] = {
            "stand_in": stand_in,
            "imputed": eligible.height - valued.height,
            "clv_per_bet": estimate(imputed, "clv_bound"),
        }
    return result


def clv_verdict(value: dict[str, Any]) -> str:
    """ADR 0032's verdict on the primary measure: favourable or unfavourable when its interval
    excludes zero, the floor holds and the bound on that side agrees; otherwise insufficient."""
    per_bet = value["clv_per_bet"]
    share = value["floor"]["share_with_proxy"]
    if "low" not in per_bet or share is None or share < FLOOR:
        return INSUFFICIENT
    low = value["bound"][f"p{BOUND[0]}"]["clv_per_bet"]
    high = value["bound"][f"p{BOUND[1]}"]["clv_per_bet"]
    if per_bet["low"] > 0 and "low" in low and low["low"] > 0:
        return FAVOURABLE
    if per_bet["high"] < 0 and "high" in high and high["high"] < 0:
        return UNFAVOURABLE
    return INSUFFICIENT


def comparisons(scored: pl.DataFrame) -> dict[str, Any]:
    """Each comparison's mean log loss difference per game, paired on the games both models
    predicted and with a result, with the games left out counted: negative when the first model
    is better."""
    result = {}
    for name, (a, b) in COMPARISONS.items():
        both = scored.filter(pl.col(a).is_not_null(), pl.col(b).is_not_null())
        paired = both.with_columns(
            difference=log_loss(pl.col(a), pl.col("home_win"))
            - log_loss(pl.col(b), pl.col("home_win"))
        )
        result[name] = {
            "difference": estimate(paired, "difference"),
            "left_out": scored.height - both.height,
        }
    return result


def _logit(p: float) -> float:
    return math.log(p / (1 - p))


def calibration_band(scored: pl.DataFrame) -> dict[str, Any]:
    """The blend's calibration on the live games: its intercept and slope, and the recalibrated
    probability at each forecast in BAND_AT against the band of BAND·p around it, all with
    weekly block bootstrap intervals."""
    rows = scored.filter(pl.col("p_blend").is_not_null())
    if not enough(rows):
        return {"games": rows.height, "weeks": weeks(rows)}
    (a, b), fitted = calibration_draws(rows, "p_blend", "home_win")
    at = []
    for p in BAND_AT:
        x = _logit(p)
        recalibrated = 1 / (1 + np.exp(-(fitted[:, 0] + fitted[:, 1] * x)))
        spread = interval(1 / (1 + math.exp(-(a + b * x))), recalibrated).to_dict()
        at.append({"forecast": p, "band": [p - BAND * p, p + BAND * p], "recalibrated": spread})
    return {
        "games": rows.height,
        "weeks": weeks(rows),
        "intercept": interval(a, fitted[:, 0]).to_dict(),
        "slope": interval(b, fitted[:, 1]).to_dict(),
        "at": at,
    }


def band_verdict(calibration: dict[str, Any]) -> str:
    """ADR 0032's verdict on the calibration band: a pass when every interval lies inside its
    band, a fail when any lies wholly outside it, and insufficient evidence otherwise."""
    if "at" not in calibration:
        return INSUFFICIENT
    at = calibration["at"]
    if all(
        r["band"][0] <= r["recalibrated"]["low"] <= r["recalibrated"]["high"] <= r["band"][1]
        for r in at
    ):
        return PASS
    if any(
        r["recalibrated"]["high"] < r["band"][0] or r["recalibrated"]["low"] > r["band"][1]
        for r in at
    ):
        return FAIL
    return INSUFFICIENT


def gaps(rows: pl.DataFrame) -> list[dict[str, Any]]:
    """The predicted games where the blend and B1 differ by more than GAP, for hand review (hard
    rule 8): never a reason to tune the model."""
    predicted = rows.filter(pl.col("status") == lp.PREDICTED)
    return (
        predicted.with_columns(gap=pl.col("p_blend") - pl.col("p_b1"))
        .filter(pl.col("gap").abs() > GAP)
        .select(
            "game_date", "game_id", "away", "home", "p_b1", "p_b3", "p_blend", "gap", "bet", "side"
        )
        .to_dicts()
    )


def drawdown(bets: pl.DataFrame, season_over: bool) -> dict[str, Any]:
    """The DRAWDOWN check on the paper bankroll, each settled bet's profit applied when its result
    became public: whether and when the bankroll first fell DRAWDOWN below its peak. The bankroll
    is the season's, so the bets are every policy's (counted with policy None). A review of
    data and code follows, never a model change. The drawdown's size, which shows the profit, only
    once the season is over (plan §11)."""
    settled = bets.filter(pl.col("settled") == ls.SETTLED)
    result: dict[str, Any] = {"threshold": DRAWDOWN, "triggered": False, "first_utc": None}
    if settled.is_empty():
        return result
    steps = settled.group_by("result_utc").agg(pl.col("profit").sum()).sort("result_utc")
    path = POLICY.bankroll + np.cumsum(steps["profit"].to_numpy())
    peaks = np.maximum.accumulate(np.concatenate([[POLICY.bankroll], path]))[1:]
    falls = (peaks - path) / peaks
    crossed = np.flatnonzero(falls >= DRAWDOWN)
    if crossed.size:
        result.update(triggered=True, first_utc=steps["result_utc"][int(crossed[0])])
    if season_over:
        result["max_drawdown"] = float(falls.max())
    return result


def u_range(rows: pl.DataFrame, means: dict[str, float], sds: dict[str, float]) -> dict[str, Any]:
    """How often live u, and each of its parts, falls far from the blend's training games (ADR
    0030): the predicted games beyond each of U_LIMITS training standard deviations. u_sd is u in
    its training standard deviations, around a training mean of 0 by construction."""
    predicted = rows.filter(pl.col("status") == lp.PREDICTED, pl.col("u_sd").is_not_null())
    beyond = {
        f"u_beyond_{limit:g}_sd": predicted.filter(pl.col("u_sd").abs() > limit).height
        for limit in U_LIMITS
    }
    for part in PARTS:
        z = (pl.col(part) - means[part]) / sds[part]
        beyond[f"{part}_beyond_{U_LIMITS[-1]:g}_sd"] = predicted.filter(
            z.abs() > U_LIMITS[-1]
        ).height
    return {"games": predicted.height, **beyond}


def sbr_history(sbr_odds: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """B0's log loss at SBR's opener (E2, its openers refused as ADR 0007 refuses them, from the
    closes of every earlier season) and at its close (E1), on HISTORY's games priced at both with
    a result: one row per game. No later season is read."""
    sbr_odds = sbr_odds.filter(pl.col("season") <= max(HISTORY))
    closes = b0(market_prices(sbr_odds, Experiment.E1), B1_METHOD)
    openers = market_prices(sbr_odds, Experiment.E2).filter(pl.col("season").is_in(HISTORY))
    kept = []
    for (season,), opened in openers.group_by("season"):
        start = closes.filter(pl.col("season") == season)["start_utc"].min()
        if not isinstance(start, datetime):
            continue
        low, high = bounds(closes, start)
        kept.append(opened.filter(~implausible(opened, low, high)))
    if not kept:
        return pl.DataFrame(
            schema={
                "season": pl.Int32,
                "game_date": pl.Date,
                "game_id": pl.Int64,
                "opener_loss": pl.Float64,
                "close_loss": pl.Float64,
            }
        )
    opened = b0(pl.concat(kept), B1_METHOD).select("game_id", p_open="p_home")
    return (
        closes.filter(pl.col("season").is_in(HISTORY))
        .select("season", "game_date", "game_id", p_close="p_home")
        .join(opened, on="game_id")
        .join(outcomes(games).select("game_id", "home_win"), on="game_id")
        .select(
            "season",
            "game_date",
            "game_id",
            opener_loss=log_loss(pl.col("p_open"), pl.col("home_win")),
            close_loss=log_loss(pl.col("p_close"), pl.col("home_win")),
        )
        .sort("game_date", "game_id")
    )


def market_timing(scored: pl.DataFrame, history: pl.DataFrame) -> dict[str, Any]:
    """ADR 0030's check: B0's log loss at Pinnacle's 12:45 price on the live games, at SBR's
    opener and close on HISTORY's, and the live figure's difference from each (different games,
    so each group is resampled on its own)."""
    live = scored.filter(pl.col("p_b0").is_not_null()).select(
        "season", "game_date", loss=log_loss(pl.col("p_b0"), pl.col("home_win"))
    )
    opener = history.select("season", "game_date", loss="opener_loss")
    close = history.select("season", "game_date", loss="close_loss")
    result: dict[str, Any] = {
        "seasons": list(HISTORY),
        "pinnacle_1245": estimate(live, "loss"),
        "sbr_opener": estimate(opener, "loss"),
        "sbr_close": estimate(close, "loss"),
    }
    if enough(live) and not history.is_empty():
        result["pinnacle_minus_opener"] = difference(live, opener, "loss").to_dict()
        result["pinnacle_minus_close"] = difference(live, close, "loss").to_dict()
    return result


def _toward(frame: pl.DataFrame, disagreement: str) -> dict[str, Any]:
    """Whether the market moved toward a disagreement at the decision: the slope of the move on
    it (0 when it says nothing about the move, 1 when the market moves all the way to the model),
    the share of games that moved its way among those where both are non-zero, and the mean move
    in its direction in points of the home side's fair probability."""
    known = frame.filter(pl.col(disagreement).is_not_null())
    leaning = known.filter(pl.col(disagreement) != 0)
    # A disagreement without spread, such as a model that always matches the market, has no
    # slope (Codex on #261).
    fitted = enough(known) and known[disagreement].n_unique() > 1
    moving = leaning.filter(pl.col("move") != 0).with_columns(
        toward=((pl.col("move") > 0) == (pl.col(disagreement) > 0)).cast(pl.Float64)
    )
    pointed = leaning.with_columns(
        points=(pl.col("p_close") - pl.col("p_decision")) * pl.col(disagreement).sign() * 100
    )
    return {
        "slope": slope(known, disagreement, "move").to_dict() | counts(known)
        if fitted
        else counts(known),
        "share_toward": estimate(moving, "toward"),
        "points_toward": estimate(pointed, "points"),
    }


def entry_to_close(moves: pl.DataFrame) -> dict[str, Any]:
    """#251's diagnostic, with no verdict, over movement.table's games with a valid close: how far
    Pinnacle's fair price moved from the decision to the close, and whether it moved toward each
    model's disagreement with it at the decision, B3's also by u below or above its training
    mean."""
    valid = moves.filter(pl.col("move").is_not_null())
    sized = valid.with_columns(points=(pl.col("p_close") - pl.col("p_decision")).abs() * 100)
    with_u = valid.filter(pl.col("u_sd").is_not_null())
    return {
        "games": valid.height,
        "unmoved": valid.filter(pl.col("move") == 0).height,
        "without_close": _counts(moves.filter(pl.col("move").is_null()), "close_status"),
        "move_points": estimate(sized, "points"),
        "models": {name: _toward(valid, f"d_{name}") for name in movement.MODELS},
        "b3_by_u": {
            "u at or below average": _toward(with_u.filter(pl.col("u_sd") <= 0), "d_B3"),
            "u above average": _toward(with_u.filter(pl.col("u_sd") > 0), "d_B3"),
        },
    }


def shadows(started: pl.DataFrame, moves: pl.DataFrame, odds: pl.DataFrame) -> dict[str, Any]:
    """#252's paper shadows over the started predicted games (live/shadow.py), with no verdict:
    the market-only control's bets against the policy's own, and the policy's bets at the best
    fresh price of the other books, each valued against the same close as the policy's."""
    keys = ["game_date", "game_id"]
    policy_bets = started.filter(pl.col("bet").fill_null(False))
    own = shadow.valued(policy_bets, moves)
    market = shadow.valued(shadow.market_only(started), moves)
    priced = shadow.best_price(policy_bets, odds, moves)
    better = priced.filter(pl.col("better_bookmaker"))
    return {
        "market_only": {
            "bets": market.height,
            "policy_bets": own.height,
            "both": own.join(market, on=keys, how="semi").height,
            "clv_per_bet": estimate(market, "clv"),
            "policy_clv_per_bet": estimate(own, "clv"),
            # The two groups share games, so resampling them apart widens the interval.
            "policy_minus_market_only": difference(own, market, "clv").to_dict()
            if enough(own) and enough(market)
            else None,
        },
        "best_price": {
            "bets": priced.height,
            "better_bookmaker": better.height,
            "better_any": priced.filter(pl.col("better_any")).height,
            "books": _counts(better, "best_bookmaker_book"),
            "clv_bookmaker": estimate(priced, "clv_bookmaker"),
            "gain_bookmaker": estimate(priced, "gain_bookmaker"),
            "clv_any": estimate(priced, "clv_any"),
            "gain_any": estimate(priced, "gain_any"),
        },
    }


def _challenger_figures(
    own: pl.DataFrame, started: pl.DataFrame, scored: pl.DataFrame, moves: pl.DataFrame
) -> dict[str, Any]:
    """A challenger's figures over its outputs own: paired log loss against the blend and B1 on
    the games with a result, the shadow bets the frozen rules would make on it with a close
    (live/shadow.py), and whether the market moved toward its disagreement at the decision
    (#251)."""
    keys = ["game_date", "game_id"]
    probability = own.filter(pl.col("p_home").is_not_null()).select(*keys, p_challenger="p_home")
    with_p = scored.join(probability, on=keys)
    compared = {}
    for name, column in (("BLEND", "p_blend"), ("B1", "p_b1")):
        both = with_p.filter(pl.col(column).is_not_null())
        paired = both.with_columns(
            difference=log_loss(pl.col("p_challenger"), pl.col("home_win"))
            - log_loss(pl.col(column), pl.col("home_win"))
        )
        compared[name] = {
            "difference": estimate(paired, "difference"),
            "left_out": with_p.height - both.height,
        }
    bets = shadow.valued(shadow.selected(started.join(probability, on=keys), "p_challenger"), moves)
    moved = (
        moves.filter(pl.col("move").is_not_null())
        .join(probability, on=keys)
        .with_columns(
            d_challenger=(pl.col("p_challenger") / (1 - pl.col("p_challenger"))).log()
            - (pl.col("p_decision") / (1 - pl.col("p_decision"))).log()
        )
    )
    return {
        "comparisons": compared,
        "shadow": {"bets": bets.height, "clv_per_bet": estimate(bets, "clv")},
        "toward_close": _toward(moved, "d_challenger"),
    }


def challengers(
    rows: pl.DataFrame,
    started: pl.DataFrame,
    scored: pl.DataFrame,
    moves: pl.DataFrame,
    outputs: pl.DataFrame,
    registry: tuple[lc.Challenger, ...],
    now: datetime,
) -> dict[str, Any]:
    """Every registered challenger, by name and version, over the counted games of the decision
    days after its registration (ADR 0038), with no verdict, failures included: its days and
    games, and its figures (_challenger_figures). Every such day counts, so a day without its
    output withholds the figures (Codex on #265): a challenger that fails on some days would
    otherwise choose its own sample. A day is pending rather than missing only until the end of
    the next UTC day by the report's clock now, since the nightly scores it the night after its
    decision."""
    keys = ["game_date", "game_id"]

    def pending(day: date) -> bool:
        return now < datetime.combine(day, time(0), UTC) + PENDING_FOR

    result = {}
    for challenger in registry:
        own = outputs.filter(
            pl.col("challenger") == challenger.name,
            pl.col("challenger_version") == challenger.version,
            pl.col("game_date") > challenger.registered_on,
        ).join(rows.select(keys), on=keys, how="semi")
        decided = rows.filter(pl.col("game_date") > challenger.registered_on)["game_date"]
        scored_days = set(own["game_date"].to_list())
        without = sorted(set(decided.to_list()) - scored_days)
        missing = [day.isoformat() for day in without if not pending(day)]
        entry: dict[str, Any] = {
            "name": challenger.name,
            "version": challenger.version,
            "adr": challenger.adr,
            "registered_on": challenger.registered_on.isoformat(),
            "days": len(scored_days),
            "games": own.height,
            "predicted": own.filter(pl.col("p_home").is_not_null()).height,
            "not_predicted": _counts(own.filter(pl.col("status") != lc.PREDICTED), "status"),
            "missing_days": missing,
            "pending_days": [day.isoformat() for day in without if pending(day)],
        }
        if not missing:
            entry |= _challenger_figures(own, started, scored, moves)
        result[f"{challenger.name}/{challenger.version}"] = entry
    return {"registered": len(registry), "challengers": result}


def real_stakes(
    value: dict[str, Any],
    season_bets: pl.DataFrame,
    rows: pl.DataFrame,
    games: pl.DataFrame,
    decided: Sequence[date],
    unfinished: Sequence[date] | None,
    drawdown_triggered: bool,
    as_of: date,
    policy: str,
) -> dict[str, Any] | None:
    """ADR 0039's start rule and weekly CLV stop for the frozen policy (live/real.py), None for an
    earlier policy's report. The paper bankroll is the season's, every policy's settled bets
    (season_bets), as the owner's veto reads it. The days the policy should have decided are the
    regular-season game dates after its freeze through as_of, from the games (Codex on #273): one
    with no ledger (decided holds every policy's ledger dates) is a day skipped without a reason,
    and only the bundles of days through as_of count. The days the policy skipped with a recorded
    reason are listed with it."""
    if policy != POLICY_VERSION:
        return None
    settled = season_bets.filter(pl.col("settled") == ls.SETTLED)
    paper = POLICY.bankroll + (float(settled["profit"].sum()) if settled.height else 0.0)
    expected = games.filter(
        (pl.col("game_id") // 10_000 % 100) == REGULAR_SEASON,
        pl.col("game_date") > FROZEN_ON,
        pl.col("game_date") <= as_of,
    )["game_date"]
    undecided = sorted(set(expected.to_list()) - set(decided))
    skipped = dict(
        rows.filter(pl.col("status").is_in(SKIPPED))
        .group_by("game_date")
        .agg(pl.col("status").first())
        .iter_rows()
    )
    through = None if unfinished is None else [day for day in unfinished if day <= as_of]
    rule = real.start_rule(
        value,
        paper,
        POLICY.bankroll,
        through,
        undecided,
        skipped,
        drawdown_triggered,
        as_of,
        real.VENUE,
    )
    return rule | {"clv_stop": real.clv_stop(value, as_of)}


def operations(rows: pl.DataFrame) -> dict[str, Any]:
    """Alerts on how the days ran, never a reason to change the policy: the days skipped and the
    games without a fresh Pinnacle price, and how often the guard stopped a picked bet."""
    picked = rows.filter(pl.col("picked").fill_null(False))
    return {
        "days": rows["game_date"].n_unique(),
        "days_skipped": rows.filter(pl.col("status").is_in(SKIPPED))["game_date"].n_unique(),
        "no_price": rows.filter(pl.col("status") == lp.NO_PRICE).height,
        "stale_price": rows.filter(pl.col("status") == lp.STALE).height,
        "missing_input": rows.filter(pl.col("status") == lp.MISSING).height,
        "picked": picked.height,
        "guarded": picked.filter(pl.col("guarded").fill_null(False)).height,
    }


def returns(bets: pl.DataFrame) -> dict[str, Any]:
    """Return at the taken price over the settled bets, for the season's end only (plan §11)."""
    settled = bets.filter(pl.col("settled") == ls.SETTLED)
    staked = float(settled["stake"].sum()) if settled.height else 0.0
    profit = float(settled["profit"].sum()) if settled.height else 0.0
    per_unit = settled.with_columns(ret=pl.col("profit") / pl.col("stake"))
    return {
        "bets": settled.height,
        "staked": staked,
        "profit": profit,
        "return_per_bet": estimate(per_unit, "ret"),
    }


def report(
    ledger: pl.DataFrame,
    settlements: pl.DataFrame,
    games: pl.DataFrame,
    odds: pl.DataFrame,
    history: pl.DataFrame,
    scale: dict[str, dict[str, float]],
    as_of: date,
    policy: str = POLICY_VERSION,
    now: datetime | None = None,
    challenger_outputs: pl.DataFrame | None = None,
    unfinished_bundles: Sequence[date] | None = None,
) -> dict[str, Any]:
    """The live report of one policy, the frozen one by default, as of as_of: ledger and
    settlements as written, games' results, the odds' Pinnacle quotes, SBR's history
    (sbr_history), the live fit's u scale (means, sds) and the registered challengers' stored
    outputs (challenger_predictions, none by default). now is the report's clock, noon UTC the
    day after as_of by default: entry to close counts the games started by then.
    unfinished_bundles are the decision days whose run bundle has no manifest, None when not
    checked, which ADR 0039's start rule reads."""
    now = now or datetime.combine(as_of + timedelta(days=1), time(12), UTC)
    rows = counted(ledger, as_of, policy)
    bets = bets_of(rows, settlements)
    # Every predicted game started by now, with a result or not yet (Codex on #261): its close
    # is final once it has started.
    started = scoreable(rows, games).filter(pl.col("start_utc") <= now)
    moves = movement.table(started, ls.closes(odds, now))
    scored = with_results(rows, games)
    formal = as_of >= REVIEW_DATE
    value = closing_value(bets)
    # The season's one paper bankroll runs through every policy (ADR 0037).
    season_bets = bets_of(counted(ledger, as_of, None), settlements)
    season_drawdown = drawdown(season_bets, formal)
    calibration = calibration_band(scored)
    cover = coverage(rows, bets, games)
    # The formal review covers every bet and game: with any still unsettled or without a
    # result, its verdicts can only be insufficient evidence.
    incomplete = {
        name: cover[name] for name in ("awaiting_settlement", "awaiting_result") if cover[name]
    }
    result: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "policy_version": policy,
        "blend_versions": sorted(set(rows["blend_version"].to_list())),
        "review_date": REVIEW_DATE.isoformat(),
        "kind": "formal review" if formal else "interim",
        "coverage": cover,
        "freshness": freshness(rows, bets, odds),
        "closing_value": value,
        "comparisons": comparisons(scored),
        "calibration": calibration,
        "gaps": gaps(rows),
        "alerts": {
            "operations": operations(rows),
            "drawdown": season_drawdown,
            "u_range": u_range(rows, scale["means"], scale["sds"]),
        },
        "market_timing": market_timing(scored, history),
        "entry_to_close": entry_to_close(moves),
        "shadows": shadows(started, moves, odds),
        "real_stakes": real_stakes(
            value,
            season_bets,
            rows,
            games,
            sorted(set(ledger.filter(pl.col("game_date") <= as_of)["game_date"].to_list())),
            unfinished_bundles,
            season_drawdown["triggered"],
            as_of,
            policy,
        ),
        "challengers": challengers(
            rows,
            started,
            scored,
            moves,
            challenger_outputs if challenger_outputs is not None else lc.empty(),
            lc.REGISTRY,
            now,
        ),
        "verdicts": {
            "closing_value": INTERIM
            if not formal
            else INSUFFICIENT
            if incomplete
            else clv_verdict(value),
            "calibration": INTERIM
            if not formal
            else INSUFFICIENT
            if incomplete
            else band_verdict(calibration),
            "incomplete": incomplete if formal else {},
        },
    }
    if formal:
        result["returns"] = returns(bets)
    return result


def _stand_in(value: float | None) -> str:
    return f"{value:+.4f}" if value is not None else f"withheld until {MIN_WEEKS} weeks"


def _fmt(figure: dict[str, Any] | None, unit: str = "games", digits: int = 4) -> str:
    """A figure as text: its value and interval, or its counts when too few weeks."""
    if figure is None:
        return "none yet"
    if "mean" in figure:
        return (
            f"{figure['mean']:+.{digits}f} [{figure['low']:+.{digits}f}, "
            f"{figure['high']:+.{digits}f}] ({figure['games']} {unit}, {figure['weeks']} weeks)"
        )
    if "value" in figure:
        low, high = figure["low"], figure["high"]
        return f"{figure['value']:+.{digits}f} [{low:+.{digits}f}, {high:+.{digits}f}]"
    return f"{figure['games']} {unit} over {figure['weeks']} weeks: too few weeks for an estimate"


def _cell(figure: dict[str, Any], digits: int = 2, scale: float = 1.0, suffix: str = "") -> str:
    """A figure for a table cell: its value and interval, or its counts when too few weeks."""
    centre = figure.get("mean", figure.get("value"))
    if centre is None:
        return f"{figure['games']} games, {figure['weeks']} weeks: too few"
    low, high = figure["low"] * scale, figure["high"] * scale
    return f"{centre * scale:+.{digits}f}{suffix} [{low:+.{digits}f}, {high:+.{digits}f}]"


def _share(n: int, of: int) -> str:
    return f"{n} of {of} ({n / of:.1%})" if of else f"{n} of 0"


def _minutes(spread: dict[str, float] | None) -> str:
    return f"median {spread['median']:.1f}, max {spread['max']:.1f}" if spread else "none yet"


def markdown(result: dict[str, Any]) -> str:
    """The report as markdown, for reports/live/."""
    cover = result["coverage"]
    value = result["closing_value"]
    alerts = result["alerts"]
    if result["kind"] == "interim":
        kind = (
            f"**Interim report.** No verdict or promotion follows from it before the formal review "
            f"on {result['review_date']} (ADR 0032). Real stakes follow only ADR 0039's start "
            "rule, below."
        )
    else:
        kind = "**The formal review** (ADR 0032)."
    lines = [
        f"# Live report, {result['as_of']}",
        "",
        kind,
        "",
        f"Policy `{result['policy_version']}`, live fit "
        + (", ".join(f"`{v}`" for v in result["blend_versions"]) or "none")
        + f". Every figure has its 95% weekly block bootstrap interval; one over fewer than "
        f"{MIN_WEEKS} weeks gives only its counts.",
        "",
        "## Coverage",
        "",
        f"- Slate games: {cover['slate_games']}, predicted {cover['predicted']}.",
    ]
    lines += [f"  - not predicted, {reason}: {n}" for reason, n in cover["not_predicted"].items()]
    lines.append(f"- Slate games awaiting a result: {cover['awaiting_result']}.")
    lines += [
        f"- Bets: {cover['bets']}: {cover['settled']} settled, "
        f"{cover['awaiting_settlement']} awaiting a result, {cover['void_postponed']} void "
        "(postponed).",
        f"- Eligible bets: {cover['eligible']}, after {cover['no_pregame_snapshot']} with no "
        f"pre-game snapshot due. With a valid closing proxy: {cover['with_proxy']}.",
    ]
    lines += [
        f"  - eligible without a proxy, {reason}: {n}"
        for reason, n in cover["eligible_without_proxy"].items()
    ]
    fresh = result["freshness"]
    lines += [
        "",
        "Quote freshness, in minutes, apart from the lead before the start:",
        "- Pinnacle's decision quote, age at the decision: "
        + _minutes(fresh["decision_quote_age_min"]),
        f"- the closing proxies, age at their snapshot: {_minutes(fresh['proxy_age_min'])}",
        f"- the closing proxies, lead before the start: {_minutes(fresh['proxy_lead_min'])}",
        "",
        "## Closing line value, the primary measure",
        "",
        "- **CLV per bet** against Pinnacle's fair closing proxy: "
        + _fmt(value["clv_per_bet"], "bets"),
        f"- Stake-weighted CLV: {_fmt(value['clv_stake_weighted'], 'bets')}",
        f"- Fair move per bet: {_fmt(value['fair_move_per_bet'], 'bets')}",
    ]
    share = value["floor"]["share_with_proxy"]
    lines.append(
        f"- Coverage floor: {share:.1%} of the eligible bets have a proxy (at least "
        f"{FLOOR:.0%} needed)."
        if share is not None
        else f"- Coverage floor: no eligible bet yet (at least {FLOOR:.0%} needed)."
    )
    for name, bound in value["bound"].items():
        lines.append(
            f"- Bound at the {name[1:]}th percentile ({_stand_in(bound['stand_in'])} for each of "
            f"{bound['imputed']} eligible bets without a proxy): "
            f"{_fmt(bound['clv_per_bet'], 'bets')}"
        )
    incomplete = result["verdicts"]["incomplete"]
    unfinished = (
        " The evidence is incomplete: "
        + ", ".join(f"{n} {name.replace('_', ' ')}" for name, n in incomplete.items())
        + "."
        if incomplete
        else ""
    )
    lines += [f"- Verdict: {result['verdicts']['closing_value']}.{unfinished}"]
    lines += _real_stakes_lines(result.get("real_stakes"))
    lines += [
        "",
        "## Model comparisons",
        "",
    ]
    lines.append(
        "Mean log loss difference per game, paired on the same games: negative favours the first."
    )
    lines.append("")
    for name, compared in result["comparisons"].items():
        left = f"; {compared['left_out']} left out" if compared["left_out"] else ""
        lines.append(f"- {name}: {_fmt(compared['difference'])}{left}")
    calibration = result["calibration"]
    lines += ["", "## Calibration of the blend", ""]
    if "at" in calibration:
        lines += [
            f"Intercept {_fmt(calibration['intercept'])}, slope {_fmt(calibration['slope'])}, "
            f"on {calibration['games']} games over {calibration['weeks']} weeks.",
            "",
            "| Forecast | Band | Recalibrated |",
            "| ---: | --- | --- |",
        ]
        lines += [
            f"| {r['forecast']:.0%} | {r['band'][0]:.2%} to {r['band'][1]:.2%} | "
            f"{r['recalibrated']['value']:.2%} [{r['recalibrated']['low']:.2%}, "
            f"{r['recalibrated']['high']:.2%}] |"
            for r in calibration["at"]
        ]
    else:
        lines.append(_fmt(calibration) + ".")
    lines += ["", f"Verdict: {result['verdicts']['calibration']}.{unfinished}", ""]
    lines += [f"## Gaps above {GAP * 100:.0f} points for hand review (hard rule 8)", ""]
    if result["gaps"]:
        lines += [
            "| Date | Game | B1 | B3 | Blend | Gap | Bet |",
            "| --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
        lines += [
            f"| {g['game_date']} | {g['away']} at {g['home']} | {g['p_b1']:.3f} | "
            f"{g['p_b3']:.3f} | {g['p_blend']:.3f} | {g['gap']:+.3f} | "
            f"{g['side'] if g['bet'] else ''} |"
            for g in result["gaps"]
        ]
    else:
        lines.append("None.")
    ops = alerts["operations"]
    dd = alerts["drawdown"]
    u = alerts["u_range"]
    lines += [
        "",
        "## Operational alerts",
        "",
        "These never change the policy.",
        "",
        f"- Days: {ops['days']}, skipped {ops['days_skipped']}. Games without Pinnacle's "
        f"midday price: {ops['no_price']}, with a stale one: {ops['stale_price']}, missing an "
        f"input: {ops['missing_input']}.",
        f"- The guard stopped {_share(ops['guarded'], ops['picked'])} picked bets.",
        (
            f"- **The {DRAWDOWN:.0%} drawdown review is triggered** "
            f"({dd['first_utc']:%Y-%m-%d}): review the data and code, never the model."
            if dd["triggered"]
            else f"- The {DRAWDOWN:.0%} drawdown review is not triggered."
        ),
    ]
    if "max_drawdown" in dd:
        lines.append(f"- Largest drawdown of the season: {dd['max_drawdown']:.1%}.")
    lines += [
        f"- u beyond {limit:g} training standard deviations: "
        f"{_share(u[f'u_beyond_{limit:g}_sd'], u['games'])} predicted games."
        for limit in U_LIMITS
    ]
    lines += [
        f"  - {part} beyond {U_LIMITS[-1]:g}: {u[f'{part}_beyond_{U_LIMITS[-1]:g}_sd']}"
        for part in PARTS
    ]
    timing = result["market_timing"]
    seasons = ", ".join(str(s) for s in timing["seasons"])
    lines += [
        "",
        "## Pinnacle at 12:45 against SBR's opener and close (ADR 0030)",
        "",
        f"B0's mean log loss per game. SBR's on {seasons}, its openers refused as ADR 0007 "
        "refuses them.",
        "",
        f"- Pinnacle at 12:45: {_fmt(timing['pinnacle_1245'])}",
        f"- SBR's opener: {_fmt(timing['sbr_opener'])}",
        f"- SBR's close: {_fmt(timing['sbr_close'])}",
    ]
    if "pinnacle_minus_opener" in timing:
        lines += [
            f"- Pinnacle minus SBR's opener: {_fmt(timing['pinnacle_minus_opener'])}",
            f"- Pinnacle minus SBR's close: {_fmt(timing['pinnacle_minus_close'])}",
        ]
    moved = result["entry_to_close"]
    missing = ", ".join(f"{reason} {n}" for reason, n in moved["without_close"].items())
    lines += [
        "",
        "## Entry to close (#251)",
        "",
        "A diagnostic with no verdict. Over the predicted games with a valid closing proxy after "
        "their decision: how far Pinnacle's fair price moved by the close, and whether it moved "
        "toward each model's disagreement with it at the decision. The close is an outcome "
        "here, never an input.",
        "",
        f"- Games: {moved['games']}, {moved['unmoved']} of them without a move. Without a "
        f"close: {missing or 'none'}.",
        f"- Mean size of the move: {_fmt(moved['move_points'], digits=2)} points of the home "
        "side's fair probability.",
        "",
        "| Disagreement at the decision | Slope of the move on it | Moves toward it | "
        "Mean move toward it, points |",
        "| --- | --- | --- | --- |",
    ]
    groups = {"B3": moved["models"]["B3"], "Blend": moved["models"]["BLEND"]} | {
        f"B3, {name}": group for name, group in moved["b3_by_u"].items()
    }
    lines += [
        f"| {name} | {_cell(g['slope'])} | {_cell(g['share_toward'], 1, 100, '%')} | "
        f"{_cell(g['points_toward'])} |"
        for name, g in groups.items()
    ]
    lines += [
        "",
        "The slope is in log-odds on both sides: 0 when the disagreement says nothing about the "
        "move, 1 when the market moves all the way to the model by the close.",
    ]
    market = result["shadows"]["market_only"]
    best = result["shadows"]["best_price"]
    gap = market["policy_minus_market_only"]
    books = ", ".join(f"{book} {n}" for book, n in best["books"].items())
    lines += [
        "",
        "## Shadow evaluations (#252)",
        "",
        "Paper shadows with no verdict, each valued against the same Pinnacle closing proxy as "
        "the policy's bets, with the ledger's predictions as written.",
        "",
        "**Market-only selection:** the frozen selection, guard and staking on the market-only "
        "control in place of the blend, at the same price, u and bankroll.",
        "",
        f"- Bets with a close: {market['bets']}, against the policy's {market['policy_bets']}; "
        f"{market['both']} games bet by both.",
        f"- CLV per bet, market-only: {_fmt(market['clv_per_bet'], 'bets')}",
        f"- CLV per bet, the policy: {_fmt(market['policy_clv_per_bet'], 'bets')}",
        "- The policy minus market-only, resampled apart: "
        + (_fmt(gap) if gap else f"withheld until {MIN_WEEKS} weeks of both"),
        "",
        "**Best-price execution:** the policy's own bets at the best fresh price of the other "
        "books when it beats Pinnacle's. An upper bound: a logged quote shows neither the stake a "
        "book would take nor that the price was still there, and an exchange's is before its "
        "commission.",
        "",
        f"- Bets with a close: {best['bets']}. A better fresh bookmaker price: "
        f"{_share(best['better_bookmaker'], best['bets'])}"
        + (f" ({books})" if books else "")
        + f"; counting the exchanges: {best['better_any']}.",
        f"- At the best bookmaker price, CLV per bet: {_fmt(best['clv_bookmaker'], 'bets')}; "
        f"gain over Pinnacle's: {_fmt(best['gain_bookmaker'], 'bets')}",
        "- With the exchanges, before their commission, CLV per bet: "
        f"{_fmt(best['clv_any'], 'bets')}; gain: {_fmt(best['gain_any'], 'bets')}",
    ]
    lines += _challenger_lines(result["challengers"])
    if "returns" in result:
        ret = result["returns"]
        lines += [
            "",
            "## Return at the taken price (the season's end only)",
            "",
            f"- {ret['bets']} settled bets, {ret['staked']:.2f} units staked, profit "
            f"{ret['profit']:+.2f} units.",
            f"- Return per bet: {_fmt(ret['return_per_bet'], 'bets')}",
        ]
    return "\n".join(lines) + "\n"


def _real_stakes_lines(section: dict[str, Any] | None) -> list[str]:
    """ADR 0039's start rule and stops as markdown lines, or none for an earlier policy."""
    if section is None:
        return []
    start, end = section["window"]
    a, c, d = (section["conditions"][name] for name in "acd")
    veto = section["veto"]

    def holds(condition: dict[str, Any]) -> str:
        return "holds" if condition["holds"] else "fails"

    share = a["share_with_proxy"]
    coverage = f"{share:.1%}" if share is not None else "none yet"
    clv = (
        f"CLV per bet {_fmt(a['clv_per_bet'], 'bets')}; pessimistic bound "
        f"{_fmt(a['bound'], 'bets')}; coverage {coverage} (at least {a['floor']:.0%})"
        if a["measured"]
        else f"not yet measured (an interval needs {MIN_WEEKS} weeks of bets)"
    )
    unfinished = c["unfinished_bundles"]
    bundles = (
        "not checked"
        if unfinished is None
        else "all complete"
        if not unfinished
        else f"unfinished on {', '.join(unfinished)}"
    )
    undecided = ", ".join(c["undecided_days"]) or "none"
    skipped = "; ".join(f"{day} ({reason})" for day, reason in c["skipped_days"].items())
    stop = section["clv_stop"]
    clv_stop = (
        "not yet measured"
        if not stop["measured"]
        else "decided at the weekly report"
        if not stop["weekly"]
        else "stop"
        if stop["stop"]
        else "pause, the coverage floor fails"
        if stop["pause"]
        else "none"
    )
    above = "above" if not veto["applies"] else "at or below"
    return [
        "",
        "## Real stakes (ADR 0039)",
        "",
        f"Real stakes start on the first decision day from {start} to {end} on which this rule "
        "holds at the weekly report (Mondays'); the paper ledger goes on either way.",
        "",
        f"**Start rule: {section['verdict']}.**",
        "",
        f"- (a) CLV, under ADR 0032's safeguards: {clv}: {holds(a)}.",
        f"- (c) Operations: run bundles {bundles}; days with games and no decision: {undecided}; "
        f"drawdown review {'triggered' if c['drawdown_triggered'] else 'not triggered'}: "
        f"{holds(c)}." + (f" Days skipped with a recorded reason: {skipped}." if skipped else ""),
        f"- (d) Venue: {d['venue'] or 'not ready (#266)'}: {holds(d)}.",
        f"- The owner's veto: the paper bankroll, {veto['paper_bankroll']:.2f} units, is {above} "
        f"its {veto['start']:.0f}, so the veto {'applies' if veto['applies'] else 'is lifted'}. "
        "A point estimate, it can block a start and never pass one.",
        f"- Weekly CLV stop: {clv_stop}. The 20% real bankroll stop is checked before every "
        "day's bets, from the real ledger (#268).",
    ]


def _challenger_lines(section: dict[str, Any]) -> list[str]:
    """The challengers section (ADR 0038) as markdown lines."""
    lines = [
        "",
        "## Challengers (ADR 0038)",
        "",
        f"Registered: {section['registered']}, each reported. Each is scored on the decision days "
        "after its registration from the day's run bundle, never as part of the decision, and "
        "with no verdict before the formal review. Every such day counts: a day without its "
        "output withholds the challenger's figures. A difference in log loss is the "
        "challenger's less the other model's, negative favouring the challenger; its shadow bets "
        "are the frozen selection, guard and staking on its probability, valued at the same "
        "closing proxy.",
    ]
    for c in section["challengers"].values():
        head = (
            f"- **{c['name']}** version {c['version']} (ADR {c['adr']}), registered "
            f"{c['registered_on']}"
        )
        pending = (
            f" Pending, scored the night after the decision: {', '.join(c['pending_days'])}."
            if c["pending_days"]
            else ""
        )
        if not c["games"] and not c["missing_days"]:
            lines += ["", f"{head}: no decision day after its registration scored yet.{pending}"]
            continue
        not_predicted = ", ".join(f"{reason} {n}" for reason, n in c["not_predicted"].items())
        lines += [
            "",
            f"{head}: {c['days']} day{'' if c['days'] == 1 else 's'}, {c['games']} games, "
            f"{c['predicted']} predicted"
            + (f"; not predicted: {not_predicted}" if not_predicted else "")
            + f".{pending}",
        ]
        if c["missing_days"]:
            lines.append(
                f"  - **Withheld:** {len(c['missing_days'])} decision days after its "
                f"registration have no output ({', '.join(c['missing_days'])}). Score them, or "
                "say why they can't be, before any figure is shown."
            )
            continue
        lines += [
            *(
                f"  - Against {model}: {_fmt(compared['difference'])}"
                + (f", {compared['left_out']} games left out" if compared["left_out"] else "")
                for model, compared in c["comparisons"].items()
            ),
            f"  - Shadow bets with a close: {c['shadow']['bets']}; CLV per bet: "
            f"{_fmt(c['shadow']['clv_per_bet'], 'bets')}",
            "  - Slope of the move to the close on its disagreement: "
            f"{_fmt(c['toward_close']['slope'])}",
        ]
    return lines


def record(result: dict[str, Any], code_version: str) -> dict[str, Any]:
    """The report as a live_reports row for the dashboard (#168): its JSON, as write() stores it,
    under its date, kind and policy."""
    return {
        "as_of": result["as_of"],
        "kind": result["kind"],
        "policy_version": result["policy_version"],
        "report": json.loads(json.dumps(result, default=str)),
        "code_version": code_version,
    }


def write(result: dict[str, Any], out: Path) -> tuple[Path, Path]:
    """The report as out/report-<as_of>.json and .md, an earlier policy's as
    out/report-<as_of>-<policy>.json and .md beside the frozen policy's."""
    out.mkdir(parents=True, exist_ok=True)
    suffix = "" if result["policy_version"] == POLICY_VERSION else f"-{result['policy_version']}"
    stem = out / f"report-{result['as_of']}{suffix}"
    json_path, md_path = stem.with_suffix(".json"), stem.with_suffix(".md")
    json_path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n")
    md_path.write_text(markdown(result))
    return json_path, md_path


def slate_markdown(day: pl.DataFrame, replacements: pl.DataFrame) -> str:
    """One day's ledger for the daily-slate review: each game's status, B1, B3 and the blend,
    the gap, u in training standard deviations and the bet; then the flags for hand review (gaps
    above GAP, guarded picks, games not predicted, u beyond U_LIMITS[0] training standard
    deviations) and the lineup gaps, the replacement skaters each team's lineup is short of."""
    lines = [
        "| Game | Status | B1 | B3 | Blend | Gap | u (sd) | Bet |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    flags = []
    for row in day.sort("start_utc", "game_id").iter_rows(named=True):
        game = f"{row['away']} at {row['home']}"
        if row["status"] != lp.PREDICTED:
            lines.append(f"| {game} | {row['status']} |  |  |  |  |  |  |")
            flags.append(f"{game}: not predicted, {row['status']}")
            continue
        gap = row["p_blend"] - row["p_b1"]
        bet = (
            f"{row['side']} at {row['price']:.2f}, EV {row['ev']:+.3f}, stake {row['stake']:.2f}"
            if row["bet"]
            else ""
        )
        lines.append(
            f"| {game} | {row['status']} | {row['p_b1']:.3f} | {row['p_b3']:.3f} | "
            f"{row['p_blend']:.3f} | {gap:+.3f} | {row['u_sd']:+.2f} | {bet} |"
        )
        if abs(gap) > GAP:
            flags.append(f"{game}: the blend is {gap:+.3f} from B1, review by hand (hard rule 8)")
        if row["picked"] and row["guarded"]:
            flags.append(
                f"{game}: the guard stopped the {row['side']} pick, its side moved "
                f"{row['moved_against']:+.3f} since the morning"
            )
        if row["u_sd"] is not None and abs(row["u_sd"]) > U_LIMITS[0]:
            flags.append(f"{game}: u is {row['u_sd']:+.2f} training standard deviations out")
    lines += ["", "Flags:"]
    lines += [f"- {flag}" for flag in flags] or ["- none"]
    short = replacements.filter(pl.col("count") > 0).sort("team", "role")
    lines += ["", "Lineup gaps, the replacement skaters each lineup is short of:"]
    lines += [
        f"- {row['team']}: {row['count']:.1f} {ROLES.get(row['role'], row['role'])}"
        for row in short.iter_rows(named=True)
    ] or ["- none"]
    return "\n".join(lines) + "\n"
