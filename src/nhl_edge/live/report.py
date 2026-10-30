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
- **The closing proxy's lead (#254),** a diagnostic with no verdict: the fair move to the proxy
  and the bets' CLV by how long before the start the proxy was taken.
- **Calibration on the selected bets (#253),** a diagnostic with no verdict: the blend's
  calibration on each game's side, and its log loss and Brier score against B1's, on the policy's
  bets against the other games, favourites against underdogs and u's two halves
  (backtest/selected.py).
- **Shadow evaluations (#252),** paper only and with no verdict: the frozen rules on the
  market-only control, and the policy's own bets at the best fresh price of the other books
  (live/shadow.py). Beside them, #308's edge bids: a resting bid at the edge price on every side
  the policy didn't bet, filled by Pinnacle's later snapshots, against the policy's own CLV.
- **Challengers (ADR 0038),** with no verdict: every registered challenger over the decision days
  after its registration (live/challengers.py), against the blend and B1 by paired log loss, and
  against its baseline challenger when its ADR names one; its shadow bets under the frozen rules,
  and its disagreement against the move to the close. At the formal review only, each
  candidate's primary measure gets its bootstrap p-value, and Holm's step-down over every
  candidate says which are candidates for a policy (ADR 0048).
- **Real stakes (ADR 0039, #267):** the start rule's conditions and verdict, with the paper
  bankroll that its owner's veto reads, and the weekly CLV stop (live/real.py). The frozen
  policy's report only.
- **Scale-up (ADR 0047, #322):** when the real bankroll's growth is read, and each fill type's
  sample of real fills so far, counts only: the CLV figures and the verdict come from the real
  fills, so they stay private (live/scale_up.py, #280). The frozen policy's report only.
- **Return at the taken price,** and the drawdown's size, only from REVIEW_DATE on (plan §11).

It reads ledgers, settlements, results and prices only: nothing here feeds a decision.
"""

import json
import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from nhl_edge.backtest import b2_report
from nhl_edge.backtest import selected as selected_bets
from nhl_edge.backtest.e3 import stake_weighted
from nhl_edge.backtest.market import Experiment, market_prices
from nhl_edge.backtest.metrics import (
    bootstrap,
    calibration_draws,
    difference,
    holm,
    interval,
    log_loss,
    p_value,
    slope,
    slope_p_value,
    week_of,
)
from nhl_edge.backtest.walk_forward import B1_METHOD, b0, bounds, implausible, outcomes
from nhl_edge.betting import staking
from nhl_edge.betting.selection import FROZEN_ON, POLICY, POLICY_VERSION, select
from nhl_edge.game.uncertainty import PARTS
from nhl_edge.ingest.games import result_public_utc
from nhl_edge.live import challengers as lc
from nhl_edge.live import morning as lm
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
# The closing proxy's lead bands' edges, in minutes (#254): up to 30, 30 to 60, and over 60. The
# pre-game slots take a proxy 15 to 75 minutes before its game (market/closing.py).
LEAD_EDGES = (30, 60)
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


def _by_lead(frame: pl.DataFrame, value: str) -> dict[str, Any]:
    """value over the frame's rows with a lead (lead_min), by LEAD_EDGES' bands, and its slope on
    the lead in hours."""
    known = frame.filter(pl.col("lead_min").is_not_null(), pl.col(value).is_not_null())
    known = known.with_columns(lead_h=pl.col("lead_min") / 60)
    edges = [None, *LEAD_EDGES, None]
    bands = []
    for lower, upper in pairwise(edges):
        inside = known
        if lower is not None:
            inside = inside.filter(pl.col("lead_min") > lower)
        if upper is not None:
            inside = inside.filter(pl.col("lead_min") <= upper)
        name = (
            f"up to {upper} min"
            if lower is None
            else f"over {lower} min"
            if upper is None
            else f"{lower} to {upper} min"
        )
        bands.append({"band": name, "estimate": estimate(inside, value)})
    fitted = enough(known) and known["lead_h"].n_unique() > 1
    return {
        "bands": bands,
        "slope_per_hour": slope(known, "lead_h", value).to_dict() | counts(known)
        if fitted
        else counts(known),
    }


# The ledger columns a game needs for its side (#253): without them it is left out.
SIDE_COLUMNS = ("side", "price", "p_side", "p_b1", "home_price", "away_price")


def selected_calibration(scored: pl.DataFrame) -> dict[str, Any]:
    """#253's splits on the games with a result (backtest/selected.py): each game on its side at
    the decision's price, the frozen policy's bets (the ledger's bet: picked and not guarded)
    against the other games, the side as favourite or underdog at that price, and u below or at
    or above its training mean. A group's figures wait for MIN_WEEKS weeks of games, its counts
    shown until then. A diagnostic with no verdict."""
    usable = scored.filter(*(pl.col(column).is_not_null() for column in SIDE_COLUMNS))
    frame = selected_bets.sides(usable, "p_b1", "bet")
    return selected_bets.splits(frame, enough) | {"without_a_side": scored.height - usable.height}


def proxy_leads(started: pl.DataFrame, odds: pl.DataFrame, now: datetime) -> pl.DataFrame:
    """Each started game's closing proxy's lead before the start, in minutes (game_date, game_id,
    lead_min): against the start the proxy was chosen by, the commence time of the game's latest
    odds snapshot (market/closing.py), not the ledger's, since the Odds API moves a start by
    minutes (Codex on #285)."""
    lead = closing.pinnacle_closes(odds, now).select(
        "event_id", lead_min=pl.col("lead").dt.total_seconds() / 60
    )
    return (
        started.select("game_date", "game_id", "event_id")
        .join(lead, on="event_id")
        .select("game_date", "game_id", "lead_min")
    )


def proxy_lead(moves: pl.DataFrame, bets: pl.DataFrame, leads: pl.DataFrame) -> dict[str, Any]:
    """#254's diagnostic, with no verdict: whether the closing proxy's lead before the start
    changes what it measures. Over movement.table's games with a valid close, the size of
    Pinnacle's fair move from the decision to the proxy, in points, by lead band and its slope on
    the lead; over the bets with a valid proxy, CLV the same way. Each lead is the proxy's own,
    from leads (proxy_leads). Late goalie news and late money arrive after an early proxy, so if
    either changes with the lead, the proxy is biased for some start times."""
    keys = ["game_date", "game_id"]
    valid = (
        moves.filter(pl.col("move").is_not_null())
        .with_columns(points=(pl.col("p_close") - pl.col("p_decision")).abs() * 100)
        .join(leads, on=keys, how="left")
    )
    valued = bets.filter(
        pl.col("settled") == ls.SETTLED, pl.col("close_status") == closing.PROXY
    ).join(leads, on=keys, how="left")
    return {"move_points": _by_lead(valid, "points"), "clv": _by_lead(valued, "clv")}


def edge_bids(
    started: pl.DataFrame,
    moves: pl.DataFrame,
    odds: pl.DataFrame,
    own: pl.DataFrame,
    dailyfaceoff: pl.DataFrame | None,
) -> dict[str, Any]:
    """#308's shadow, with no verdict: a bid at the edge price on every side of the started games
    the policy didn't bet (live/shadow.py), its fills from Pinnacle's later snapshots valued at
    the bid against the same close as the policy's own bets (own, valued), and the share of fills
    after Daily Faceoff's named starter changed. Each waits for MIN_WEEKS weeks of fills."""
    bids = shadow.edge_bids(started)
    filled = shadow.edge_fills(bids, odds).filter(pl.col("fill_utc").is_not_null())
    valued = shadow.valued(filled.with_columns(price=pl.col("bid")), moves)
    changed = None
    if dailyfaceoff is not None:
        # Over the fills with both teams' starters named at both moments, as a share with its
        # weekly block bootstrap interval, counts only under MIN_WEEKS (Codex on #313).
        known = (
            shadow.starter_changes(filled, dailyfaceoff)
            .filter(pl.col("starter_changed").is_not_null())
            .with_columns(changed=pl.col("starter_changed").cast(pl.Float64))
        )
        changed = estimate(known, "changed")
    return {
        "bids": bids.height,
        "filled": filled.height,
        "slots": _counts(filled, "fill_slot"),
        "with_close": valued.height,
        "clv_per_fill": estimate(valued, "clv"),
        "fair_move_per_fill": estimate(valued, "fair_move"),
        "policy_clv_per_bet": estimate(own, "clv"),
        "starter_changed": changed,
    }


def shadows(
    started: pl.DataFrame,
    moves: pl.DataFrame,
    odds: pl.DataFrame,
    dailyfaceoff: pl.DataFrame | None = None,
) -> dict[str, Any]:
    """#252's paper shadows over the started predicted games (live/shadow.py), with no verdict:
    the market-only control's bets against the policy's own, and the policy's bets at the best
    fresh price of the other books, each valued against the same close as the policy's; and
    #308's edge bids (edge_bids), with Daily Faceoff's polls when given."""
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
        "edge_bids": edge_bids(started, moves, odds, own, dailyfaceoff),
    }


def _paired(frame: pl.DataFrame, formal: bool) -> dict[str, Any]:
    """A challenger's paired difference with its interval, and at the formal review, once it has
    one, its two-sided p-value from the same weekly block bootstrap (ADR 0048)."""
    figure = estimate(frame, "difference")
    if formal and enough(frame):
        figure["p_value"] = p_value(frame, "difference")
    return figure


def _challenger_figures(
    own: pl.DataFrame,
    started: pl.DataFrame,
    scored: pl.DataFrame,
    moves: pl.DataFrame,
    forecasts: str = lc.RESULT,
    formal: bool = False,
) -> dict[str, Any]:
    """A challenger's figures over its outputs own: paired log loss against the blend and B1 on
    the games with a result, the shadow bets the frozen rules would make on it with a close
    (live/shadow.py), and whether the market moved toward its disagreement at the decision
    (#251). A challenger that forecasts the close (ADR 0049) is scored on the move to it
    (_move_figures), and its shadow bets are the policy's own, kept where its forecast close
    clears the price."""
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
            "difference": _paired(paired, formal),
            "left_out": with_p.height - both.height,
        }
    if forecasts == lc.CLOSE:
        p_side = pl.when(pl.col("side") == "home").then(pl.col("p_challenger"))
        kept = (
            shadow.selected(started, "p_blend")
            .join(probability, on=keys)
            .filter(p_side.otherwise(1 - pl.col("p_challenger")) * pl.col("price") - 1 >= 0)
        )
        bets = shadow.valued(kept, moves)
    else:
        bets = shadow.valued(
            shadow.selected(started.join(probability, on=keys), "p_challenger"), moves
        )
    moved = (
        moves.filter(pl.col("move").is_not_null())
        .join(probability, on=keys)
        .with_columns(
            d_challenger=(pl.col("p_challenger") / (1 - pl.col("p_challenger"))).log()
            - (pl.col("p_decision") / (1 - pl.col("p_decision"))).log()
        )
    )
    figures = {
        "comparisons": compared,
        "shadow": {"bets": bets.height, "clv_per_bet": estimate(bets, "clv")},
        "toward_close": _toward(moved, "d_challenger"),
    }
    if forecasts == lc.CLOSE:
        figures[lc.MOVE] = _move_figures(moved, formal)
    return figures


def _move_figures(moved: pl.DataFrame, formal: bool = False) -> dict[str, Any]:
    """The primary measure of a challenger that forecasts the close (ADR 0049), over the games
    with one: (y - ŷ)² - y², y the move to the close and ŷ the forecast's (d_challenger), both in
    the home side's log-odds. Its forecast's squared error less that of "no move": negative
    favours the challenger."""
    frame = moved.with_columns(
        difference=(pl.col("move") - pl.col("d_challenger")) ** 2 - pl.col("move") ** 2
    )
    return {"difference": _paired(frame, formal)}


def _against_baseline(
    own: pl.DataFrame,
    baseline: pl.DataFrame,
    name: str,
    scored: pl.DataFrame,
    spreads: pl.DataFrame | None,
    formal: bool = False,
) -> dict[str, Any]:
    """A challenger's paired log loss against its baseline challenger's outputs (ADR 0042) on
    the games both predicted with a result, and on those of them with a goalie spread above 0
    when the spreads are given: the challenger's less the baseline's."""
    keys = ["game_date", "game_id"]
    mine = own.filter(pl.col("p_home").is_not_null()).select(*keys, p_challenger="p_home")
    theirs = baseline.filter(pl.col("p_home").is_not_null()).select(*keys, p_baseline="p_home")
    with_p = scored.join(mine, on=keys)
    paired = with_p.join(theirs, on=keys).with_columns(
        difference=log_loss(pl.col("p_challenger"), pl.col("home_win"))
        - log_loss(pl.col("p_baseline"), pl.col("home_win"))
    )
    left_out = with_p.height - paired.height
    result = {name: {"difference": _paired(paired, formal), "left_out": left_out}}
    if spreads is not None:
        spread = spreads.filter(pl.col("goalie_spread") > 0).select(keys)
        within = paired.join(spread, on=keys, how="semi")
        result[f"{name}, goalie spread above 0"] = {
            "difference": _paired(within, formal),
            "left_out": with_p.join(spread, on=keys, how="semi").height - within.height,
        }
    return result


def challengers(
    rows: pl.DataFrame,
    started: pl.DataFrame,
    scored: pl.DataFrame,
    moves: pl.DataFrame,
    outputs: pl.DataFrame,
    registry: tuple[lc.Challenger, ...],
    now: datetime,
    spreads: pl.DataFrame | None = None,
    formal: bool = False,
) -> dict[str, Any]:
    """Every registered challenger, by name and version, over the counted games of the decision
    days after its registration (ADR 0038), with no verdict, failures included: its days and
    games, and its figures (_challenger_figures). Every such day counts, so a day without its
    output withholds the figures (Codex on #265): a challenger that fails on some days would
    otherwise choose its own sample. A day is pending rather than missing only until the end of
    the next UTC day by the report's clock now, since the nightly scores it the night after its
    decision. A challenger with a baseline is also paired with it (_against_baseline), and on the
    goalie spreads (goalie_spreads) when they are given and it reads them, unless the baseline's
    own outputs are missing for some of those days. At the formal review (formal), the review
    tests every candidate's primary measure under Holm's step-down (review, ADR 0048)."""
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
        if challenger.baseline is not None:
            entry["baseline"] = challenger.baseline
        # A challenger that fits (ADR 0041) shows its latest day's fit, unfitted or not.
        fits = own.filter(pl.col("fit").is_not_null()).sort("game_date")
        if fits.height:
            entry["latest_fit"] = json.loads(fits["fit"][-1])
        if not missing:
            entry["forecasts"] = challenger.forecasts
            entry |= _challenger_figures(own, started, scored, moves, challenger.forecasts, formal)
            baseline = lc.empty()
            if challenger.baseline in lc.LEDGER_BASELINES:
                # A ledger model (ADR 0044): the day's own column, never missing a day.
                column = lc.LEDGER_BASELINES[challenger.baseline]
                baseline = scored.select(*keys, p_home=column)
                entry["baseline_missing_days"] = []
            elif challenger.baseline is not None:
                name, version = challenger.baseline.split("/")
                baseline = outputs.filter(
                    pl.col("challenger") == name, pl.col("challenger_version") == version
                )
                # The baseline's own missing days withhold the pairing too (ADR 0038 rule 2):
                # its failures would otherwise choose the primary measure's sample.
                lacking = sorted(set(decided.to_list()) - set(baseline["game_date"].to_list()))
                entry["baseline_missing_days"] = [
                    day.isoformat() for day in lacking if not pending(day)
                ]
            if challenger.baseline is not None and not entry["baseline_missing_days"]:
                # The goalie-spread subset is the measure of a challenger that reads them.
                subset = spreads if "goalie_spreads" in challenger.reads else None
                entry["comparisons"] |= _against_baseline(
                    own, baseline, challenger.baseline, scored, subset, formal
                )
        result[f"{challenger.name}/{challenger.version}"] = entry
    section: dict[str, Any] = {"registered": len(registry), "challengers": result}
    if formal:
        section["review"] = review(result, registry)
    return section


# The review's family-wise level for the challengers (ADR 0048).
REVIEW_ALPHA = 0.05


def _primary(entry: dict[str, Any], primary: str) -> dict[str, Any] | None:
    """A challenger's primary measure's figure from its entry, None when withheld."""
    if primary == lc.MOVE:
        return (entry.get(lc.MOVE) or {}).get("difference")
    return (entry.get("comparisons") or {}).get(primary, {}).get("difference")


def review(entries: dict[str, Any], registry: tuple[lc.Challenger, ...]) -> dict[str, Any]:
    """ADR 0048 at the formal review: Holm's step-down at family-wise REVIEW_ALPHA over every
    registered challenger with a primary measure, the control (market_only) aside. A challenger
    whose measure is withheld or has no interval yet counts in the family with p = 1, so it can't
    pass, nor be dropped to ease the others. One that passes is a candidate only if its
    difference favours it (below 0)."""
    family: dict[str, float] = {}
    figures: dict[str, dict[str, Any] | None] = {}
    for challenger in registry:
        if challenger.primary is None:
            continue
        name = f"{challenger.name}/{challenger.version}"
        figures[name] = _primary(entries[name], challenger.primary)
        family[name] = (figures[name] or {}).get("p_value", 1.0)
    tested = holm(family, REVIEW_ALPHA)
    for name, result in tested.items():
        favours = figures[name] is not None and figures[name].get("mean", 0.0) < 0  # type: ignore[union-attr]
        result |= {"favours": favours, "candidate": result["passes"] and favours}
    return {"family": len(family), "alpha": REVIEW_ALPHA, "challengers": tested}


def _morning_bets(started: pl.DataFrame) -> pl.DataFrame:
    """The bets the frozen selection and staking would make at 07:05 on the morning blend, at
    Pinnacle's morning price and the season's opening bankroll, without the guard: no earlier pair
    gives it a move to read (ADR 0052)."""
    chosen = select(started.with_columns(p_home=pl.col("p_blend")), POLICY).drop("p_home")
    bets = chosen.filter(pl.col("picked"))
    return staking.fractions(bets, POLICY).with_columns(
        bankroll=pl.lit(POLICY.bankroll), stake=pl.col("fraction") * POLICY.bankroll
    )


def _logit_of(column: str) -> pl.Expr:
    return (pl.col(column) / (1 - pl.col(column))).log()


def morning_view(
    morning: pl.DataFrame,
    rows: pl.DataFrame,
    games: pl.DataFrame,
    odds: pl.DataFrame,
    as_of: date,
    now: datetime,
    formal: bool = False,
) -> dict[str, Any]:
    """The morning view (ADR 0052) over its stored regular-season days after its registration up
    to as_of, with no verdict before the formal review. Its primary measure: the slope of
    Pinnacle's move from the morning snapshot to the close on B3's disagreement with it there,
    over the predicted games started by now with a valid close (movement.table, as #251 takes the
    decision's), with its p-value at the formal review. Beside it, entry_to_close's other figures
    from the morning; the move split at the decision (rows, the counted ledger), each leg's slope
    on B3's morning disagreement; paired log loss at 07:05 against B0 and against the decision's
    blend; and the shadow bets at 07:05 (_morning_bets), valued at the close. A game day of the
    ledger without its morning is listed, never withheld: the run ends before any move or
    result exists, so its failures can't choose its sample."""
    keys = ["game_date", "game_id"]
    counted_days = (pl.col("game_date") > lm.REGISTERED_ON) & (pl.col("game_date") <= as_of)
    stored = morning.filter(
        pl.col("morning_version") == lm.VERSION,
        (pl.col("game_id") // 10_000 % 100) == REGULAR_SEASON,
        counted_days,
    )
    days = set(stored["game_date"].to_list())
    game_days = sorted(set(rows.filter(counted_days)["game_date"].to_list()))
    section: dict[str, Any] = {
        "version": lm.VERSION,
        "registered_on": lm.REGISTERED_ON.isoformat(),
        "days": len(days),
        "games": stored.height,
        "predicted": stored.filter(pl.col("status") == lm.PREDICTED).height,
        "not_predicted": _counts(stored.filter(pl.col("status") != lm.PREDICTED), "status"),
        "missing_days": [d.isoformat() for d in game_days if d not in days],
    }
    started = scoreable(stored, games).filter(pl.col("start_utc") <= now)
    moves = movement.table(
        started.rename({"morning_snapshot_utc": "decision_snapshot_utc"}), ls.closes(odds, now)
    )
    entry = entry_to_close(moves)
    valid = moves.filter(pl.col("move").is_not_null())
    primary = dict(entry["models"]["B3"]["slope"])
    if formal and "value" in primary:
        primary["p_value"] = slope_p_value(valid, "d_B3", "move")
    decided = scoreable(rows, games).select(
        *keys, "decision_snapshot_utc", p_at_decision="p_b0", p_decision_blend="p_blend"
    )
    # Both legs on the same games: those whose close came after the decision's own snapshot, as
    # the decision's entry to close takes it, so neither leg counts a close it never moved to.
    both = valid.join(decided, on=keys, suffix="_decision").filter(
        pl.col("close_snapshot_utc") > pl.col("decision_snapshot_utc_decision")
    )
    legs = {
        "07:05 to the decision": both.with_columns(
            move=_logit_of("p_at_decision") - _logit_of("p_decision"),
            p_close=pl.col("p_at_decision"),
        ),
        "the decision to the close": both.with_columns(
            move=_logit_of("p_close") - _logit_of("p_at_decision"),
            p_decision=pl.col("p_at_decision"),
        ),
    }
    scored = with_results(stored, games).join(decided, on=keys, how="left")
    pairs = {
        "B3 against B0, both at 07:05": ("p_b3", "p_b0"),
        "The blend against B0, both at 07:05": ("p_blend", "p_b0"),
        "The blend at 07:05 against the decision's": ("p_blend", "p_decision_blend"),
    }
    compared = {}
    for name, (mine, other) in pairs.items():
        paired = scored.filter(pl.col(other).is_not_null()).with_columns(
            difference=log_loss(pl.col(mine), pl.col("home_win"))
            - log_loss(pl.col(other), pl.col("home_win"))
        )
        compared[name] = estimate(paired, "difference")
    bets = shadow.valued(_morning_bets(started), moves)
    return section | {
        "primary": primary,
        "entry_to_close": entry,
        "split": {name: _toward(leg, "d_B3") for name, leg in legs.items()},
        "comparisons": compared,
        "shadow": {"bets": bets.height, "clv_per_bet": estimate(bets, "clv")},
    }


def real_stakes(
    bets: pl.DataFrame,
    season_bets: pl.DataFrame,
    rows: pl.DataFrame,
    games: pl.DataFrame,
    decided: Sequence[date],
    unfinished: Sequence[date] | None,
    as_of: date,
    now: datetime,
    policy: str,
) -> dict[str, Any] | None:
    """ADR 0039's start rule and weekly CLV stop for the frozen policy (live/real.py), None for an
    earlier policy's report. They read only the settlements whose result was public by now, the
    report's clock (Codex on #273): the policy's bets (bets) for the CLV, and every policy's
    (season_bets) for the season's one paper bankroll, which the owner's veto and the drawdown
    review read. A weekly report decides once the previous day's results were public by now.
    The days the policy should have decided are the regular-season game dates after its freeze
    through as_of, from the games: one with no ledger (decided holds every policy's ledger dates)
    is a day skipped without a reason, and only the bundles of days through as_of count. The days
    the policy skipped with a recorded reason are listed with it."""
    if policy != POLICY_VERSION:
        return None
    public = pl.col("result_utc") <= now
    value = closing_value(bets.filter(public))
    known = season_bets.filter(public)
    settled = known.filter(pl.col("settled") == ls.SETTLED)
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
    complete = now >= result_public_utc(as_of - timedelta(days=1))
    rule = real.start_rule(
        value,
        paper,
        POLICY.bankroll,
        through,
        undecided,
        skipped,
        drawdown(known, False)["triggered"],
        as_of,
        real.VENUE,
        complete,
    )
    return rule | {"clv_stop": real.clv_stop(value, as_of, complete)}


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
    goalie_spreads: pl.DataFrame | None = None,
    dailyfaceoff: pl.DataFrame | None = None,
    scale_up: dict[str, Any] | None = None,
    morning: pl.DataFrame | None = None,
) -> dict[str, Any]:
    """The live report of one policy, the frozen one by default, as of as_of: ledger and
    settlements as written, games' results, the odds' Pinnacle quotes, SBR's history
    (sbr_history), the live fit's u scale (means, sds) and the registered challengers' stored
    outputs (challenger_predictions, none by default). now is the report's clock, noon UTC the
    day after as_of by default: entry to close counts the games started by then.
    unfinished_bundles are the decision days whose run bundle has no manifest, None when not
    checked, which ADR 0039's start rule reads. dailyfaceoff is Daily Faceoff's goalie polls,
    which #308's edge bids read for the share of fills after a starter changed. scale_up is ADR
    0047's public counts (live/scale_up.public), None when not read. morning is the stored
    morning views (morning_predictions, ADR 0052), none by default."""
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
            "drawdown": drawdown(season_bets, formal),
            "u_range": u_range(rows, scale["means"], scale["sds"]),
        },
        "market_timing": market_timing(scored, history),
        "entry_to_close": entry_to_close(moves),
        "proxy_lead": proxy_lead(moves, bets, proxy_leads(started, odds, now)),
        "selected_calibration": selected_calibration(scored),
        "shadows": shadows(started, moves, odds, dailyfaceoff),
        "real_stakes": real_stakes(
            bets,
            season_bets,
            rows,
            games,
            sorted(set(ledger.filter(pl.col("game_date") <= as_of)["game_date"].to_list())),
            unfinished_bundles,
            as_of,
            now,
            policy,
        ),
        "scale_up": scale_up if policy == POLICY_VERSION else None,
        "challengers": challengers(
            rows,
            started,
            scored,
            moves,
            challenger_outputs if challenger_outputs is not None else lc.empty(),
            lc.REGISTRY,
            now,
            goalie_spreads,
            formal,
        ),
        "morning_view": morning_view(
            morning if morning is not None else lm.empty(),
            rows,
            games,
            odds,
            as_of,
            now,
            formal,
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
            f"on {result['review_date']} (ADR 0032)."
            + (
                " Real stakes follow only ADR 0039's start rule, below."
                if result.get("real_stakes") is not None
                else ""
            )
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
    lines += _scale_up_lines(result.get("scale_up"))
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
    lead = result["proxy_lead"]
    lines += [
        "",
        "## The closing proxy's lead (#254)",
        "",
        "A diagnostic with no verdict. The closing proxy is the last fresh Pinnacle quote at most "
        "90 minutes before the start, so the pre-game slots take it 15 to 75 minutes before its "
        "game, and late news can arrive after it. If what it measures changes with its lead, the "
        "proxy is biased for some start times.",
        "",
        "| Proxy's lead | Fair move from the decision to the proxy, points | CLV per bet |",
        "| --- | --- | --- |",
    ]
    lines += [
        f"| {move['band']} | {_cell(move['estimate'])} | {_cell(clv['estimate'], 4)} |"
        for move, clv in zip(lead["move_points"]["bands"], lead["clv"]["bands"], strict=True)
    ]
    lines += [
        "",
        f"- Slope on the lead, per hour: the move {_cell(lead['move_points']['slope_per_hour'])}, "
        f"CLV {_cell(lead['clv']['slope_per_hour'], 4)}.",
    ]
    lines += _selected_lines(result["selected_calibration"])
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
    lines += _edge_bid_lines(result["shadows"]["edge_bids"])
    lines += _challenger_lines(result["challengers"])
    if "morning_view" in result:
        lines += _morning_lines(result["morning_view"])
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


def _edge_bid_lines(edge: dict[str, Any]) -> list[str]:
    """#308's shadow: the bids, their fills by slot, the fills' CLV beside the policy's and the
    share filled after a starter changed."""
    slots = ", ".join(f"{slot} {n}" for slot, n in edge["slots"].items())
    changed = edge["starter_changed"]
    named = (
        f"{_fmt(changed, 'fills', 3)}, over the fills with Daily Faceoff's starters named at both"
        if changed is not None
        else "not read"
    )
    return [
        "",
        "**Edge bids on every game (#308):** a resting bid on every side the policy didn't bet, "
        "at the lowest price to the cent where the blend clears the side's hurdle, filled at the "
        "bid by the first later fresh Pinnacle quote at or above it before the start. Five slots "
        "a day undercount the fills, and a logged quote shows no stake: an upper bound. Fewer "
        f"than {MIN_WEEKS} weeks of fills decide nothing.",
        "",
        f"- Bids: {edge['bids']}; filled: {_share(edge['filled'], edge['bids'])}"
        + (f" ({slots})" if slots else "")
        + f"; with a close: {edge['with_close']}.",
        f"- CLV per fill: {_fmt(edge['clv_per_fill'], 'fills')}; the policy's per bet: "
        f"{_fmt(edge['policy_clv_per_bet'], 'bets')}",
        f"- Fair move per fill: {_fmt(edge['fair_move_per_fill'], 'fills')}",
        f"- Filled after a named starter changed: {named}.",
    ]


def _selected_lines(section: dict[str, Any]) -> list[str]:
    """#253's section: each split's two groups, with their figures once they have MIN_WEEKS
    weeks of games, and their counts until then."""
    lines = [
        "",
        "## Calibration on the selected bets (#253)",
        "",
        "A diagnostic with no verdict. Each game is scored on its side of higher expected return "
        "at the decision's price. A blend calibrated over every game can still be over-confident "
        "where it disagrees with the market enough to bet. Log loss and Brier are against B1 on "
        "the same games (negative favours the blend); the slope is 1 when calibrated, below 1 "
        "when over-confident.",
        "",
        "| Split | Group | Games | Log loss - B1 | Brier - B1 | Calibration slope |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, (_, yes, no) in selected_bets.SPLITS.items():
        for group in (yes, no):
            figure = section[name][group]
            games = f"{figure['games']} over {figure['weeks']} weeks"
            if "log_loss_minus_b1" not in figure:
                lines.append(f"| {name} | {group} | {games} | too few weeks | | |")
                continue
            slope = _cell(figure["slope"]) if "slope" in figure else "no fit"
            lines.append(
                f"| {name} | {group} | {games} | {_cell(figure['log_loss_minus_b1'], 4)} | "
                f"{_cell(figure['brier_minus_b1'], 4)} | {slope} |"
            )
    left = section["u"]["left_out"]
    lines += [
        "",
        f"- {section['without_a_side']} games without a side at the decision's price are left "
        f"out, and {left} more from the u split for want of u.",
    ]
    return lines


def _real_stakes_lines(section: dict[str, Any] | None) -> list[str]:
    """ADR 0039's start rule and stops as markdown lines, or none for an earlier policy."""
    if section is None:
        return []
    start, end = section["window"]
    a, c, d = (section["conditions"][name] for name in "acd")
    veto = section["veto"]

    def holds(condition: dict[str, Any]) -> str:
        # Unmeasured is not a failure (hard rule 7, Codex on #293): the condition doesn't hold yet.
        if condition["holds"]:
            return "holds"
        return "does not hold yet" if not condition.get("measured", True) else "does not hold"

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
        f"- (c) Operations: run bundles {bundles}; days with games and no decision: {undecided}: "
        f"{holds(c)}." + (f" Days skipped with a recorded reason: {skipped}." if skipped else ""),
        f"- (d) Venue: {d['venue'] or 'not ready (#266)'}: {holds(d)}.",
        f"- The owner's veto: the paper bankroll, {veto['paper_bankroll']:.2f} units, is {above} "
        f"its {veto['start']:.0f}, so the veto {'applies' if veto['applies'] else 'is lifted'}. "
        "A point estimate, it can block a start and never pass one.",
        "- The drawdown review (ADR 0032): "
        + (
            "open, the paper bankroll fell 20% from a peak, so it blocks a start until reviewed."
            if section["drawdown_review"]["applies"]
            else "not triggered."
        ),
        f"- Weekly CLV stop: {clv_stop}. The 20% real bankroll stop is checked before every "
        "day's bets, from the real ledger (#268).",
    ]


def _scale_up_lines(section: dict[str, Any] | None) -> list[str]:
    """ADR 0047's reading date and samples as markdown lines, counts only, or none when the
    report didn't read the real record."""
    if section is None:
        return []
    day = section["reading_date"]
    when = (
        "no real fill yet: it is read on the Monday "
        f"{section['weeks_needed']} weeks after the first real fill's week"
        if day is None
        else f"read on {day}, at the weekly report's clock"
        if section["read"]
        else f"due on {day}: it is taken once every order of a game before its clock has its "
        "fills recorded"
        if section["due"]
        else f"read once, on {day}"
    )
    lines = [
        "",
        "## Scale-up (ADR 0047)",
        "",
        "The real bankroll may grow, at the owner's choice, once one reading shows net CLV per "
        f"real fill with its interval's lower bound above 0, over at least "
        f"{section['weeks_needed']} weeks of fills with at least "
        f"{section['coverage_needed']:.0%} of them referenced after the fill, and no stop fired; "
        "taker and maker fills apart. The CLV figures and the verdict come from the real fills, "
        "so they stay private and go to the owner (#280).",
        "",
        f"- Reading: {when}.",
    ]
    for kind, sample in section["samples"].items():
        share = sample["coverage"]
        lines.append(
            f"- {kind.capitalize()} fills: {sample['fills']} over {sample['weeks']} weeks; "
            f"{sample['valued']} of {sample['started']} of started games with a reference after "
            f"them ({f'{share:.0%}' if share is not None else 'none yet'})."
        )
    return lines


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
        if c.get("latest_fit"):
            lines.append(_fit_line(c["latest_fit"]))
        if c["missing_days"]:
            lines.append(
                f"  - **Withheld:** {len(c['missing_days'])} decision days after its "
                f"registration have no output ({', '.join(c['missing_days'])}). Score them, or "
                "say why they can't be, before any figure is shown."
            )
            continue
        close = c.get("forecasts") == lc.CLOSE
        if close:
            lines.append(
                "  - Its forecast of the move to the close, squared error less no move's (its "
                f"primary measure, ADR 0049): {_fmt(c['move']['difference'])}"
            )
        lines += [
            *(
                f"  - Against {model}: {_fmt(compared['difference'])}"
                + (f", {compared['left_out']} games left out" if compared["left_out"] else "")
                for model, compared in c["comparisons"].items()
            ),
            (
                "  - The policy's own bets its forecast close clears, with a close: "
                if close
                else "  - Shadow bets with a close: "
            )
            + f"{c['shadow']['bets']}; CLV per bet: {_fmt(c['shadow']['clv_per_bet'], 'bets')}",
            "  - Slope of the move to the close on its disagreement: "
            f"{_fmt(c['toward_close']['slope'])}",
        ]
        if c.get("baseline_missing_days"):
            days = c["baseline_missing_days"]
            lines.append(
                f"  - **Against {c['baseline']}: withheld.** Its outputs are missing for "
                f"{len(days)} decision days ({', '.join(days)}), so the pairing would be on games "
                "its failures chose."
            )
    if "review" in section:
        lines += _review_lines(section["review"])
    return lines


def _morning_lines(section: dict[str, Any]) -> list[str]:
    """The morning view's section (ADR 0052) as markdown lines."""
    lines = [
        "",
        "## The morning view (ADR 0052)",
        "",
        f"Version {section['version']}, registered {section['registered_on']}: the decision's "
        "models at the 07:05 ET snapshot, with no bet, from inputs known before it, with no "
        "verdict before the formal review. Its primary measure is the slope of Pinnacle's move "
        "from that snapshot to the close on B3's disagreement with it there, in log-odds: above "
        "0 when the market later prices in what B3 sees in the morning. A game day without its "
        "morning is listed, not withheld: the run ends before any move or result exists.",
        "",
    ]
    if not section["games"] and not section["missing_days"]:
        return [*lines, "No game day after its registration yet."]
    not_predicted = ", ".join(f"{reason} {n}" for reason, n in section["not_predicted"].items())
    lines.append(
        f"- Days: {section['days']}, {section['games']} games, {section['predicted']} predicted"
        + (f"; not predicted: {not_predicted}" if not_predicted else "")
        + "."
    )
    if section["missing_days"]:
        lines.append(
            f"- Without a stored morning: {len(section['missing_days'])} game days "
            f"({', '.join(section['missing_days'])})."
        )
    primary = section["primary"]
    lines.append(
        f"- **The slope on B3's disagreement (primary):** {_fmt(primary)}"
        + (f", p = {primary['p_value']:.4f}" if "p_value" in primary else "")
    )
    moved = section["entry_to_close"]
    groups = {"B3": moved["models"]["B3"], "Blend": moved["models"]["BLEND"]} | {
        f"B3, {name}": group for name, group in moved["b3_by_u"].items()
    }
    lines += [
        f"- Games with a close: {moved['games']}; mean size of the move "
        f"{_fmt(moved['move_points'], digits=2)} points.",
        "",
        "| Disagreement at 07:05 | Slope of the move on it | Moves toward it | "
        "Mean move toward it, points |",
        "| --- | --- | --- | --- |",
        *(
            f"| {name} | {_cell(g['slope'])} | {_cell(g['share_toward'], 1, 100, '%')} | "
            f"{_cell(g['points_toward'])} |"
            for name, g in groups.items()
        ),
        "",
        "The move split at the decision, each leg's slope on B3's disagreement at 07:05:",
        *(f"- {name}: {_fmt(leg['slope'])}" for name, leg in section["split"].items()),
        "",
        "Paired log loss on the results, the first model's less the second's (negative "
        "favours the first):",
        *(f"- {name}: {_fmt(figure)}" for name, figure in section["comparisons"].items()),
        "",
        f"- Shadow bets at 07:05 with a close: {section['shadow']['bets']}; CLV per bet: "
        f"{_fmt(section['shadow']['clv_per_bet'], 'bets')}",
    ]
    return lines


def _review_lines(review: dict[str, Any]) -> list[str]:
    """ADR 0048's step-down at the formal review as markdown lines, in rank order."""
    lines = [
        "",
        f"**The review's test (ADR 0048):** Holm's step-down over the {review['family']} "
        f"registered candidates at family-wise {review['alpha']:.0%}, each on its primary "
        "measure's two-sided p-value from its own weekly block bootstrap. A measure withheld or "
        "without an interval counts with p = 1. A candidate passes and its difference favours "
        "it; it enters a policy only through a new version, its ADR, its freeze and the owner's "
        "decision.",
        "",
    ]
    for name, result in sorted(review["challengers"].items(), key=lambda item: item[1]["rank"]):
        outcome = (
            "a candidate"
            if result["candidate"]
            else "passes, but its difference doesn't favour it"
            if result["passes"]
            else "does not pass"
        )
        lines.append(
            f"- {name}: p = {result['p_value']:.4f}, threshold {result['threshold']:.4f}: "
            f"{outcome}."
        )
    return lines


def _fit_line(fit: dict[str, Any]) -> str:
    """A challenger's latest fit: its weights with their standard errors, or the games it still
    lacks."""
    if "weights" not in fit:
        return (
            f"  - Week of {fit['week']}: unfitted, on {fit['games']} training games of the "
            f"{fit['minimum']} it needs."
        )
    terms = ", ".join(
        f"{term} {weight:+.3f} (SE {fit['standard_errors'][term]:.3f})"
        for term, weight in fit["weights"].items()
    )
    return f"  - Latest fit, week of {fit['week']}, on {fit['games']} games: {terms}."


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
