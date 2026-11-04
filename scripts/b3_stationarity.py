"""#378 step 1: is the mapping from B3's inputs to the full-game result stable across the training
seasons? A diagnostic for whether recency weighting of B3's final logistic has anything to track.

**The fit,** per training season from 2012-13 to 2017-18 (SEASONS): B3's logistic (ADR 0023) on
that season's own games, as B3 trains (b3.training_games: Δĝ from the goalies who started, the
schedule terms and empty seats, h_s as the offset), with no penalty. B3's frozen L2 would shrink a
720-game season more than a 1,230-game one and make drift by construction. The full-game result,
overtime and the shootout included (hard rule 2).

**Reported per season:** Δĝ's coefficient per goal, the intercept at zero inputs (the home bias
beyond h_s), and the coefficient over the pooled six-season fit's, each with a weekly block
bootstrap interval within the season (hard rule 7).

**The tests,** fixed on #378 before the run: Cochran's Q on the six coefficients and on the six
intercepts, each weighted by its bootstrap variance, against chi-squared with 5 degrees of freedom,
with Holm's adjustment over the two. Step 2 goes ahead if either adjusted p is below ALPHA.

**What it reads:** the lake to 2017-18 only, as the yardstick reads it (ys.read's tables, cut and
read at their data's time). No development, flagged or held-out season. Its settings were tuned on
these seasons (ADR 0011): a descriptive check of stability, not a holdout.
"""

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from scipy.stats import chi2

from nhl_edge.backtest import metrics, reports
from nhl_edge.backtest import yardstick as ys
from nhl_edge.game import b2, b3
from nhl_edge.lake.tables import Lake

SEASONS = (20122013, 20132014, 20142015, 20152016, 20162017, 20172018)
LAST = max(SEASONS)
UNPENALIZED = b2.Settings(l2=0.0)
ALPHA = 0.05
SKILL = b3.INPUTS.index("delta_g_hat")


@dataclass(frozen=True)
class Fit:
    games: int
    coefficient: float
    intercept: float


def read_rows(lake: Any, last: int = LAST) -> pl.DataFrame:
    """Every game of the seasons to last as B3 trains on it: its inputs with Δĝ from its
    starters, offset, home_win, season and game_date."""
    games = lake.read("games").filter(pl.col("season") <= last)
    players = b3.Tables(
        games=games,
        schedule_terms=lake.read("schedule_terms"),
        goalie_starts=lake.read("goalie_starts"),
        actual_lineups=lake.read("actual_lineups"),
        **{name: lake.read(name) for name in ys.PLAYER_TABLES},
    )
    tables = ys.through(players, last)
    inputs = b3.with_terms(tables, b3.game_inputs(tables), b3.V1)
    # Every game of the seasons to last is complete: train as the next season's fold would.
    end = tables.games["observed_utc"].max()
    assert isinstance(end, datetime)
    rows = b3.training_games(tables, inputs, last + 10001, end + timedelta(days=1), "observed_utc")
    dates = games.select("game_id", "game_date")
    return rows.join(dates, on="game_id").filter(pl.col("season").is_in(list(SEASONS)))


def fit(rows: pl.DataFrame, season: int) -> Fit:
    """B3's logistic on rows, unpenalized: Δĝ's coefficient per goal and the intercept at zero
    inputs, both in the inputs' own units."""
    model = b3.fit(rows, UNPENALIZED, season)
    weights, means, scales = (np.asarray(v) for v in (model.weights, model.means, model.scales))
    intercept = model.intercept - float(np.sum(weights * means / scales))
    return Fit(rows.height, float(weights[SKILL] / scales[SKILL]), float(intercept))


def bootstrap(
    rows: pl.DataFrame, season: int, draws: int = metrics.DRAWS, seed: int = metrics.SEED
) -> np.ndarray:
    """Weekly block bootstrap within the season: each draw resamples its weeks with replacement
    and refits; one row per draw of (coefficient, intercept)."""
    weeks = rows.with_columns(week=metrics.week_of(pl.col("game_date")))
    blocks = [frame for _, frame in weeks.sort("week").group_by("week", maintain_order=True)]
    rng = np.random.default_rng(seed)
    out = np.empty((draws, 2))
    for i in range(draws):
        picked = pl.concat([blocks[j] for j in rng.integers(0, len(blocks), len(blocks))])
        found = fit(picked, season)
        out[i] = (found.coefficient, found.intercept)
    return out


def cochran_q(values: Sequence[float], variances: Sequence[float]) -> dict[str, float]:
    """Cochran's Q of values with their variances: the weighted spread around their weighted
    mean, against chi-squared with one fewer degrees of freedom than values."""
    v, w = np.asarray(values, float), 1 / np.asarray(variances, float)
    mean = float(np.sum(w * v) / np.sum(w))
    q = float(np.sum(w * (v - mean) ** 2))
    return {"q": q, "df": len(v) - 1, "p_value": float(chi2.sf(q, len(v) - 1)), "mean": mean}


def run(rows: pl.DataFrame, draws: int = metrics.DRAWS) -> dict[str, Any]:
    """Each season's fit with its intervals, the pooled fit, and the two tests with Holm. Only
    the rows of SEASONS are read."""
    rows = rows.filter(pl.col("season").is_in(list(SEASONS)))
    pooled = fit(rows, LAST)
    per_season: dict[str, Any] = {}
    coefficients, intercepts, coef_var, icpt_var = [], [], [], []
    for season in SEASONS:
        of = rows.filter(pl.col("season") == season)
        found = fit(of, season)
        draws_ = bootstrap(of, season, draws)
        coefficient = metrics.interval(found.coefficient, draws_[:, 0])
        intercept = metrics.interval(found.intercept, draws_[:, 1])
        ratio = metrics.interval(
            found.coefficient / pooled.coefficient, draws_[:, 0] / pooled.coefficient
        )
        per_season[str(season)] = {
            "games": found.games,
            "coefficient": coefficient.to_dict(),
            "intercept": intercept.to_dict(),
            "over_pooled": ratio.to_dict(),
        }
        coefficients.append(found.coefficient)
        intercepts.append(found.intercept)
        coef_var.append(float(np.var(draws_[:, 0], ddof=1)))
        icpt_var.append(float(np.var(draws_[:, 1], ddof=1)))
    tests = {
        "coefficient": cochran_q(coefficients, coef_var),
        "intercept": cochran_q(intercepts, icpt_var),
    }
    holm = metrics.holm({name: t["p_value"] for name, t in tests.items()}, ALPHA)
    return {
        "seasons": list(SEASONS),
        "draws": draws,
        "pooled": {
            "games": pooled.games,
            "coefficient": pooled.coefficient,
            "intercept": pooled.intercept,
        },
        "per_season": per_season,
        "tests": tests,
        "holm": holm,
        "step_2": any(h["passes"] for h in holm.values()),
    }


def _cell(found: Mapping[str, float], digits: int = 3) -> str:
    return f"{found['value']:+.{digits}f} [{found['low']:+.{digits}f}, {found['high']:+.{digits}f}]"


def markdown(found: Mapping[str, Any], version: str) -> str:
    def season(s: int) -> str:
        return f"{s // 10000}-{s % 100:02d}"

    lines = [
        f"# B3's mapping across the training seasons ({version})",
        "",
        "#378 step 1, with its rule fixed on the issue before this run. B3's logistic is fitted on",
        "each training season's own games, as B3 trains (Δĝ from the starters, schedule terms,",
        "empty seats, h_s as the offset), without a penalty. Intervals are weekly block bootstrap",
        f"intervals within each season ({found['draws']} draws). The settings behind the inputs",
        "were tuned on these seasons (ADR 0011), so this is a descriptive check of stability.",
        "",
        "| Season | Games | Δĝ's coefficient per goal | Over the pooled fit's "
        "| Intercept at zero inputs |",
        "|---|---|---|---|---|",
    ]
    for key, row in found["per_season"].items():
        lines.append(
            f"| {season(int(key))} | {row['games']:,} | {_cell(row['coefficient'])} | "
            f"{_cell(row['over_pooled'], 2)} | {_cell(row['intercept'])} |"
        )
    pooled = found["pooled"]
    lines += [
        f"| Pooled | {pooled['games']:,} | {pooled['coefficient']:+.3f} | 1 "
        f"| {pooled['intercept']:+.3f} |",
        "",
        "**The tests** (Cochran's Q, each value weighted by its bootstrap variance; Holm over the "
        "two):",
        "",
        "| Test | Q | df | p | Holm threshold | Heterogeneous |",
        "|---|---|---|---|---|---|",
    ]
    for name, test in found["tests"].items():
        holm = found["holm"][name]
        lines.append(
            f"| {name} | {test['q']:.2f} | {test['df']} | {test['p_value']:.3f} | "
            f"{holm['threshold']:.3f} | {'yes' if holm['passes'] else 'no'} |"
        )
    verdict = (
        "**Step 2 goes ahead:** at least one test finds the mapping heterogeneous across the "
        "training seasons."
        if found["step_2"]
        else "**Step 2 stops:** neither test finds the mapping heterogeneous across the training "
        "seasons, so under #378's rule the issue closes as rejected. The drift seen from 2018-19 "
        "on is not read here."
    )
    return "\n".join([*lines, "", verdict, ""])


def main() -> None:
    parser = argparse.ArgumentParser(description="B3's mapping across the training seasons (#378).")
    parser.add_argument("--out", type=Path, default=Path("reports/backtest"))
    args = parser.parse_args()
    found = run(read_rows(Lake()))
    version = reports.version("b3-stationarity", datetime.now(UTC))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{version}.json").write_text(json.dumps(found, indent=2, sort_keys=True))
    path = args.out / f"{version}.md"
    path.write_text(markdown(found, version))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
