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
- **Return at the taken price,** and the drawdown's size, only from REVIEW_DATE on (plan §11).

It reads ledgers, settlements, results and prices only: nothing here feeds a decision.
"""

import json
import math
from datetime import date, datetime
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
    week_of,
)
from nhl_edge.backtest.walk_forward import B1_METHOD, b0, bounds, implausible, outcomes
from nhl_edge.betting.selection import POLICY, POLICY_VERSION
from nhl_edge.game.uncertainty import PARTS
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


def counted(ledger: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """The ledger rows the evidence counts: the frozen policy's regular-season slate games up to
    as_of (ADR 0032; a new policy version starts its own record)."""
    return ledger.filter(
        pl.col("policy_version") == POLICY_VERSION,
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


def estimate(frame: pl.DataFrame, value: str) -> dict[str, Any]:
    """value's mean over the frame's games with its weekly block bootstrap interval, or only the
    counts when they span fewer than MIN_WEEKS weeks."""
    if not enough(frame):
        return {"games": frame.height, "weeks": weeks(frame)}
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
        "clv_stake_weighted": stake_weighted(valued) if enough(valued) else None,
        "fair_move_per_bet": estimate(valued, "fair_move"),
        "floor": {"required": FLOOR, "share_with_proxy": share},
        "bound": {},
    }
    if valued.is_empty():
        return result
    for percentile in BOUND:
        stand_in = float(np.percentile(valued["clv"].to_numpy(), percentile))
        imputed = eligible.with_columns(clv_bound=pl.col("clv").fill_null(stand_in))
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
    became public: whether and when the bankroll first fell DRAWDOWN below its peak. A review of
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
) -> dict[str, Any]:
    """The live report as of as_of: ledger and settlements as written, games' results, the odds'
    Pinnacle quotes, SBR's history (sbr_history) and the live fit's u scale (means, sds)."""
    rows = counted(ledger, as_of)
    bets = bets_of(rows, settlements)
    scored = with_results(rows, games)
    formal = as_of >= REVIEW_DATE
    value = closing_value(bets)
    calibration = calibration_band(scored)
    cover = coverage(rows, bets, games)
    # The formal review covers every bet and game: with any still unsettled or without a
    # result, its verdicts can only be insufficient evidence.
    incomplete = {
        name: cover[name] for name in ("awaiting_settlement", "awaiting_result") if cover[name]
    }
    result: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "policy_version": POLICY_VERSION,
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
            "drawdown": drawdown(bets, formal),
            "u_range": u_range(rows, scale["means"], scale["sds"]),
        },
        "market_timing": market_timing(scored, history),
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
            f"**Interim report.** No verdict, promotion or real stake follows from it: the formal "
            f"review is on {result['review_date']} (ADR 0032)."
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
        f"- Stake-weighted CLV: {_fmt(value['clv_stake_weighted'])}",
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
            f"- Bound at the {name[1:]}th percentile ({bound['stand_in']:+.4f} for each of "
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
    lines += [
        f"- Verdict: {result['verdicts']['closing_value']}.{unfinished}",
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
    """The report as out/report-<as_of>.json and .md."""
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"report-{result['as_of']}"
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
