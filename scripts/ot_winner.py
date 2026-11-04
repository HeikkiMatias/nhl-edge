"""#379 step 1: is the overtime or shootout winner predictable beyond a constant? The gate of the
score model (plan §5, #15), with its rules fixed on the issue before the run (the issue and its
comment of 2026-11-04).

    uv run python scripts/ot_winner.py

**Rows:** regular-season games tied after regulation, by the period 1 to 3 goals of `shots`,
cross-checked with `decided_in` (a game where they disagree is dropped and counted). The label is
the home team winning in overtime or the shootout: never read from a regulation score, since a
shootout adds a goal for its winner.

**Seasons:** the 3-on-3 era from 2015-16 (plan §5). Scored: 2016-17, 2017-18, 2018-19 and 2021-22
(SCORED), each fitted on the earlier 3-on-3 seasons; 2019-20 and 2020-21 are training rows only.
Every one of these seasons has been seen, so this is development evidence. 2022-23 and later are
not read.

**Candidates** (fixed on the issue):
- q0 = 0.5;
- q1: the home rate in the earlier seasons' ties, shrunk toward 0.5 by PRIOR_TIES;
- q2 = sigmoid(a + c·o), o the de-vigged SBR opener's log-odds at E2's time (ys.market), by
  maximum a posteriori with a ~ N(0, 1²) and c ~ N(0, 0.5²);
- q3 = sigmoid(a + c·Δĝ), Δĝ at E2's time from the yardstick's inputs, with c ~ N(0, 1²): on
  2016-17 and 2017-18 only, where those inputs exist (ADR 0011).

**Measures:** the paired log loss of each of q1 to q3 against q0 on the ties both scored, with a
weekly block bootstrap interval and Holm's adjustment over the three (the primary measure);
beside it the paired Brier, (q - y)², from 0 to 1, q's calibration and the counts. **The gate:**
q2 or q3 against q1, Holm over the two, an interval below 0, and an implied full-game shift
D̂·(q - q1) of a point or more on at least SHARE of every priced game of the scored seasons, D̂
the earlier 3-on-3 seasons' draw rate.

It reads the lake to 2021-22 only and writes reports/backtest/ot-winner-<yyyymmdd>-<shortsha>.md.
"""

import argparse
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray
from scipy.optimize import minimize
from scipy.special import expit

from nhl_edge.backtest import metrics, reports
from nhl_edge.backtest import yardstick as ys
from nhl_edge.lake.tables import Lake

FIRST = 20152016
LAST = 20212022
SCORED = (20162017, 20172018, 20182019, 20212022)
Q3_SCORED = (20162017, 20172018)
PRIOR_TIES = 300.0
PRIOR_A = 1.0
PRIOR_C = {"q2": 0.5, "q3": 1.0}
INPUT = {"q2": "o", "q3": "delta_g_hat"}
ALPHA = 0.05
SHIFT, SHARE = 0.01, 0.10
EPS = 1e-12


def ties(games: pl.DataFrame, shots: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    """The 3-on-3 era's games tied after regulation by their period 1 to 3 goals, with
    decided_in agreeing (the count of games where it does not), and home_won: the home team won
    in overtime or the shootout."""
    era = games.filter(pl.col("season").is_between(FIRST, LAST))
    goals = (
        shots.filter(pl.col("is_goal"), pl.col("period") <= 3)
        .group_by("game_id")
        .agg(home_goals=pl.col("is_home").sum(), away_goals=(~pl.col("is_home")).sum())
    )
    joined = era.join(goals, on="game_id", how="left").with_columns(
        pl.col("home_goals", "away_goals").fill_null(0)
    )
    level = pl.col("home_goals") == pl.col("away_goals")
    extra = pl.col("decided_in") != "REG"
    disagree = joined.filter(level != extra).height
    tied = joined.filter(level & extra).select(
        "game_id",
        "season",
        "game_date",
        home_won=(pl.col("home_score") > pl.col("away_score")).cast(pl.Int8),
    )
    return tied, disagree


def draw_rates(games: pl.DataFrame) -> dict[int, float]:
    """Each season's share of games decided after regulation, in the 3-on-3 era."""
    era = games.filter(pl.col("season").is_between(FIRST, LAST))
    rates = era.group_by("season").agg(rate=(pl.col("decided_in") != "REG").mean())
    return dict(rates.select("season", "rate").iter_rows())


def shrunk_rate(train: pl.DataFrame) -> float:
    """q1: the home team's rate in train's ties, shrunk toward 0.5 by PRIOR_TIES."""
    wins = float(train["home_won"].sum())
    return (wins + PRIOR_TIES / 2) / (train.height + PRIOR_TIES)


def map_logistic(
    x: NDArray[np.float64], y: NDArray[np.float64], prior_c: float
) -> tuple[float, float]:
    """(a, c) of P(y = 1) = sigmoid(a + c·x) at the posterior's mode, a ~ N(0, PRIOR_A²) and
    c ~ N(0, prior_c²)."""

    def loss(beta: NDArray[np.float64]) -> tuple[float, NDArray[np.float64]]:
        eta = beta[0] + beta[1] * x
        p = expit(eta)
        nll = -np.sum(y * np.log(p + EPS) + (1 - y) * np.log(1 - p + EPS))
        penalty = beta[0] ** 2 / (2 * PRIOR_A**2) + beta[1] ** 2 / (2 * prior_c**2)
        grad = np.array(
            [
                -np.sum(y - p) + beta[0] / PRIOR_A**2,
                -np.sum((y - p) * x) + beta[1] / prior_c**2,
            ]
        )
        return float(nll + penalty), grad

    result = minimize(loss, np.zeros(2), jac=True, method="L-BFGS-B")
    if not result.success:
        raise ValueError(f"the fit did not converge: {result.message}")
    return float(result.x[0]), float(result.x[1])


def predictions(
    tied: pl.DataFrame,
    inputs: pl.DataFrame,
    games: pl.DataFrame,
    rates: Mapping[int, float],
) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, Any]]:
    """Each scored season's ties with q0 to q3 (null where a candidate has no input), every
    priced game of the scored seasons with q1 to q3 and D̂ for the implied shift, and each fit.
    Every fit reads only the earlier seasons."""
    rows = tied.join(inputs, on="game_id", how="left")
    priced = (
        games.filter(pl.col("season").is_in(list(SCORED)))
        .select("game_id", "season")
        .join(inputs, on="game_id")
    )
    scored_ties, scored_games, fits = [], [], {}
    for season in SCORED:
        train = rows.filter(pl.col("season") < season)
        q1 = shrunk_rate(train)
        earlier = [rates[s] for s in rates if FIRST <= s < season]
        draw = float(np.mean(earlier))
        fits[str(season)] = {"ties_trained": train.height, "q1": q1, "draw_rate": draw}
        test = rows.filter(pl.col("season") == season).with_columns(q0=pl.lit(0.5), q1=pl.lit(q1))
        everyone = priced.filter(pl.col("season") == season).with_columns(
            q1=pl.lit(q1), draw=pl.lit(draw)
        )
        for name, column in INPUT.items():
            if name == "q3" and season not in Q3_SCORED:
                test = test.with_columns(pl.lit(None, dtype=pl.Float64).alias(name))
                everyone = everyone.with_columns(pl.lit(None, dtype=pl.Float64).alias(name))
                continue
            usable = train.filter(pl.col(column).is_not_null())
            a, c = map_logistic(
                usable[column].to_numpy().astype(float),
                usable["home_won"].to_numpy().astype(float),
                PRIOR_C[name],
            )
            fits[str(season)][name] = {"a": a, "c": c, "ties_trained": usable.height}
            # Nulls stay null: a game without the input has no prediction.
            value = 1 / (1 + (-(a + c * pl.col(column))).exp())
            test = test.with_columns(value.alias(name))
            everyone = everyone.with_columns(value.alias(name))
        scored_ties.append(test)
        scored_games.append(everyone)
    return pl.concat(scored_ties, how="diagonal"), pl.concat(scored_games, how="diagonal"), fits


def _log_loss(p: pl.Expr, y: pl.Expr) -> pl.Expr:
    p = p.clip(EPS, 1 - EPS)
    return -(y * p.log() + (1 - y) * (1 - p).log())


def paired(frame: pl.DataFrame, a: str, b: str) -> pl.DataFrame:
    """a's log loss and Brier less b's, tie by tie, on the ties both have."""
    y = pl.col("home_won").cast(pl.Float64)
    return frame.filter(pl.col(a).is_not_null(), pl.col(b).is_not_null()).with_columns(
        log_loss=_log_loss(pl.col(a), y) - _log_loss(pl.col(b), y),
        brier=(pl.col(a) - y) ** 2 - (pl.col(b) - y) ** 2,
    )


def measures(frame: pl.DataFrame, a: str, b: str) -> dict[str, Any]:
    both = paired(frame, a, b)
    return {
        "ties": both.height,
        "log_loss": metrics.bootstrap(both, "log_loss").to_dict(),
        "log_loss_p": metrics.p_value(both, "log_loss"),
        "brier": metrics.bootstrap(both, "brier").to_dict(),
    }


def shift_share(games: pl.DataFrame, name: str) -> dict[str, Any]:
    """The share of priced games whose full-game probability q's candidate would move by SHIFT or
    more from q1's, D̂·|q - q1|."""
    known = games.filter(pl.col(name).is_not_null())
    moved = (pl.col("draw") * (pl.col(name) - pl.col("q1")).abs()) >= SHIFT
    return {"games": known.height, "share": float(known.select(moved.mean()).item() or 0.0)}


def run(
    tied: pl.DataFrame, inputs: pl.DataFrame, games: pl.DataFrame, rates: Mapping[int, float]
) -> dict[str, Any]:
    """The primary measure, the gate and the figures beside them."""
    scored, priced, fits = predictions(tied, inputs, games, rates)
    against_q0 = {name: measures(scored, name, "q0") for name in ("q1", "q2", "q3")}
    holm = metrics.holm({n: m["log_loss_p"] for n, m in against_q0.items()}, ALPHA)
    against_q1 = {name: measures(scored, name, "q1") for name in ("q2", "q3")}
    gate_holm = metrics.holm({n: m["log_loss_p"] for n, m in against_q1.items()}, ALPHA)
    shifts = {name: shift_share(priced, name) for name in ("q2", "q3")}
    beats = {
        name: gate_holm[name]["passes"]
        and against_q1[name]["log_loss"]["high"] < 0
        and shifts[name]["share"] >= SHARE
        for name in ("q2", "q3")
    }
    calibration = {
        name: {
            k: v.to_dict()
            for k, v in metrics.calibration(
                scored.filter(pl.col(name).is_not_null()), name, "home_won"
            ).items()
        }
        for name in ("q2", "q3")
    }
    per_season = (
        scored.group_by("season")
        .agg(ties=pl.len(), home_rate=pl.col("home_won").mean(), q1=pl.col("q1").first())
        .sort("season")
    )
    return {
        "fits": fits,
        "against_q0": against_q0,
        "holm": holm,
        "against_q1": against_q1,
        "gate_holm": gate_holm,
        "shifts": shifts,
        "calibration": calibration,
        "per_season": per_season.to_dicts(),
        "gate": any(beats.values()),
        "gate_by": beats,
    }


def markdown(found: Mapping[str, Any], disagree: int, version: str) -> str:
    def cell(e: Mapping[str, Any], scale: float = 1000.0) -> str:
        mid = e["mean"] if "mean" in e else e["value"]
        return f"{mid * scale:+.2f} [{e['low'] * scale:+.2f}, {e['high'] * scale:+.2f}]"

    def season(s: int) -> str:
        return f"{s // 10000}-{s % 100:02d}"

    lines = [
        f"# The overtime and shootout winner ({version})",
        "",
        "#379 step 1, its rules fixed on the issue before the run. Ties after regulation by their",
        "period 1 to 3 goals, the label the home team winning in overtime or the shootout, in the",
        "3-on-3 era. Each scored season is fitted on the earlier ones. Log loss and Brier in",
        "thousandths, the candidate's less the baseline's, tie by tie: negative favours the",
        "candidate. Intervals are 95% weekly block bootstrap. Every season read has been seen,",
        "so this is development evidence.",
        "",
        f"Games where the regulation goals and `decided_in` disagree, dropped: {disagree}.",
        "",
        "| Season | Ties | Home won | q1 |",
        "|---|---|---|---|",
    ]
    for row in found["per_season"]:
        lines.append(
            f"| {season(row['season'])} | {row['ties']} | {row['home_rate']:.3f} | "
            f"{row['q1']:.3f} |"
        )
    lines += [
        "",
        "**The primary measure,** each candidate against q0 = 0.5 (Holm over the three):",
        "",
        "| Candidate | Ties | Log loss | Brier | p | Holm threshold | Passes |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, m in found["against_q0"].items():
        h = found["holm"][name]
        lines.append(
            f"| {name} | {m['ties']} | {cell(m['log_loss'])} | {cell(m['brier'])} | "
            f"{m['log_loss_p']:.3f} | {h['threshold']:.3f} | {'yes' if h['passes'] else 'no'} |"
        )
    lines += [
        "",
        "**The gate,** q2 and q3 against q1 (Holm over the two), and the share of priced games",
        f"whose full-game probability they would move by {SHIFT:.0%} or more from q1's:",
        "",
        "| Candidate | Ties | Log loss | Brier | p | Holm threshold | Shift share | Passes |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, m in found["against_q1"].items():
        h, s = found["gate_holm"][name], found["shifts"][name]
        lines.append(
            f"| {name} | {m['ties']} | {cell(m['log_loss'])} | {cell(m['brier'])} | "
            f"{m['log_loss_p']:.3f} | {h['threshold']:.3f} | {s['share']:.1%} of {s['games']:,} | "
            f"{'yes' if found['gate_by'][name] else 'no'} |"
        )
    lines += ["", "**q's calibration** (intercept 0 and slope 1 when calibrated):", ""]
    for name, c in found["calibration"].items():
        lines.append(f"- {name}: intercept {cell(c['intercept'], 1)}, slope {cell(c['slope'], 1)}")
    lines += [
        "",
        "**Step 2 goes ahead** (a new issue): the gate passes."
        if found["gate"]
        else "**Step 2 does not go ahead:** the gate does not pass. q stays implicit in B3; a "
        "regulation or totals market would need the owner's scope first.",
        "",
    ]
    return "\n".join(lines)


def read(lake: Any) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, dict[int, float], int]:
    """The lake to 2021-22: the ties, every game's E2 opener (o) and, on the training seasons,
    Δĝ at E2's time, the games, the draw rates and the count of disagreeing games."""
    games = lake.read("games").filter(pl.col("season") <= LAST)
    shots = lake.read("shots").filter(pl.col("season").is_between(FIRST, LAST))
    tied, disagree = ties(games, shots)
    sbr = lake.read("sbr_odds").filter(pl.col("season") <= LAST)
    opens = ys.market(sbr, games).select("game_id", "o")
    training = ys.read(lake, max(Q3_SCORED)).frame.select("game_id", "delta_g_hat")
    inputs = opens.join(training, on="game_id", how="left")
    return tied, inputs, games, draw_rates(games), disagree


def main() -> None:
    parser = argparse.ArgumentParser(description="The overtime and shootout winner (#379).")
    parser.add_argument("--out", type=Path, default=Path("reports/backtest"))
    args = parser.parse_args()
    tied, inputs, games, rates, disagree = read(Lake())
    found = run(tied, inputs, games, rates)
    version = reports.version("ot-winner", datetime.now(UTC))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{version}.json").write_text(
        json.dumps(found | {"disagree": disagree}, indent=2, sort_keys=True, default=str)
    )
    path = args.out / f"{version}.md"
    path.write_text(markdown(found, disagree, version))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
