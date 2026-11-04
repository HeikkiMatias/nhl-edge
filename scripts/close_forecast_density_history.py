"""ADR 0056: close_forecast version 2's evidence on the training seasons, its stop rule, and its
frozen weights. It reads the yardstick's rows (ADR 0053) to 2017-18 and writes
reports/yardstick/close-forecast-2-history-<yyyymmdd>-<shortsha>.md. It writes nothing to the lake.

    uv run python scripts/close_forecast_density_history.py [--artifact reports/live]

Version 2 is version 1's split regression (scripts/close_forecast_history.py, ADR 0054) with #370's
five schedule density columns beside B3's four parts: the move from SBR's opener to its close, in
the home side's log-odds, y_hat = a + b_o*o + b*parts + b_d*density. Density is counted at E2's
prediction time from the season's games whose results were public by then (features/density.py).

1. **The frozen fit:** its weights on every game from 2012-13 to 2017-18, the fit the live
   version 2 carries, train_cutoff 2018-04-09.
2. **Out of sample, beside version 1 on the same games:** each season from 2013-14 fitted on the
   earlier ones (ys.scored), each version's squared error less no move's (ADR 0054's measure),
   and version 2's less version 1's, on every game, the rested games and the back-to-back games.
3. **The stop (ADR 0056):** unless version 2 less version 1 is below 0 on the rested games, its
   interval's upper end below 0, the build stops and returns to the owner.
4. **The forecast moves' sizes, and taking at the opener,** for both versions, as version 1's
   report reads them.

Every interval is a weekly block bootstrap (hard rule 7). The features' settings were tuned on
these seasons (ADR 0011), and #370 chose density on 2013-14 and 2014-15: the figures are
development evidence, and the confirmation is live.
"""

import argparse
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from close_forecast_history import (
    REVIEW,
    THRESHOLDS,
    against_no_move,
    opener_prices,
    sizes,
    takes,
)
from numpy.typing import NDArray

from nhl_edge.backtest import metrics, reports
from nhl_edge.backtest import yardstick as ys
from nhl_edge.features.density import DENSITY, density
from nhl_edge.game import b3
from nhl_edge.lake.tables import Lake

PARTS = b3.SPLIT
COLUMNS = (*PARTS, *DENSITY)
WEIGHTS = ("a", "b_o", *COLUMNS)
VERSIONS = {"1": PARTS, "2": COLUMNS}
SUBSETS = ("every", "rested", "back_to_back")


def with_density(read: ys.Read) -> pl.DataFrame:
    """The yardstick's rows with B3's parts and the home edge, each with its density columns at
    its prediction time."""
    frame = read.frame.drop_nulls(["y", "o", *PARTS, "h_s"])
    moments = frame.select("game_id", "prediction_utc")
    return frame.join(density(read.games, moments), on="game_id").sort("game_id")


def design(rows: pl.DataFrame) -> NDArray[np.float64]:
    """The fit's columns: 1, o, the four parts and the five density columns."""
    return np.column_stack(
        [np.ones(rows.height), rows["o"].to_numpy(), *(rows[c].to_numpy() for c in COLUMNS)]
    ).astype(np.float64)


def frozen_fit(rows: pl.DataFrame) -> dict[str, float]:
    """Version 2's weights by least squares on every row, as ys.scored fits a season."""
    beta, *_ = np.linalg.lstsq(design(rows), rows["y"].to_numpy(), rcond=None)
    return dict(zip(WEIGHTS, (float(b) for b in beta), strict=True))


def out_of_sample(frame: pl.DataFrame, seasons: tuple[int, ...]) -> pl.DataFrame:
    """Each scored game with both versions' squared error less no move's (s_1, s_2), their
    forecast moves (y_hat_1, y_hat_2) and version 2's less version 1's (difference), on the games
    both forecast."""
    found = None
    for version, columns in VERSIONS.items():
        games = against_no_move(ys.scored(frame, columns, seasons)).select(
            "game_id",
            "season",
            "game_date",
            "rested",
            "y",
            **{f"s_{version}": "s", f"y_hat_{version}": "y_hat"},
        )
        found = (
            games
            if found is None
            else found.join(
                games.select("game_id", f"s_{version}", f"y_hat_{version}"), on="game_id"
            )
        )
    assert found is not None
    return found.with_columns(difference=pl.col("s_2") - pl.col("s_1")).sort("game_id")


def subset(games: pl.DataFrame, name: str) -> pl.DataFrame:
    """The games of a subset: every game, the rested ones (neither team played the day before) or
    the rest, where a team did."""
    if name == "rested":
        return games.filter(pl.col("rested"))
    if name == "back_to_back":
        return games.filter(~pl.col("rested"))
    return games


def stop(rested: Mapping[str, float]) -> bool:
    """ADR 0056's stop: true unless version 2 less version 1 on the rested games is below 0, the
    upper end of its interval below 0."""
    return not rested["high"] < 0


def artifact(weights: Mapping[str, float], fitted: pl.DataFrame, version: str) -> dict[str, Any]:
    """Version 2's frozen weights (ADR 0056): the fit on fitted, with its games, seasons and
    train_cutoff, the last result it read."""
    cutoff = fitted["result_utc"].max()
    assert isinstance(cutoff, datetime)
    return {
        "component": "close-forecast-2",
        "version": version,
        "adr": "0056",
        "source": "scripts/close_forecast_density_history.py",
        "target": "SBR's move from the opener to the close, home log-odds",
        "inputs": ["1", "o", *COLUMNS],
        "split": ["a", "b_o", *PARTS],
        "density": list(DENSITY),
        "weights": dict(weights),
        "games": fitted.height,
        "seasons": sorted(fitted["season"].unique().to_list()),
        "train_cutoff": cutoff.isoformat(),
    }


def write_artifact(
    directory: Path, weights: Mapping[str, float], fitted: pl.DataFrame, version: str
) -> Path:
    """The artifact as JSON under directory, named by its version."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{version}.json"
    path.write_text(json.dumps(artifact(weights, fitted, version), indent=2, sort_keys=True) + "\n")
    return path


def _cell(estimate: Mapping[str, Any], scale: float = 1.0, digits: int = 3) -> str:
    m, lo, hi = (estimate[k] * scale for k in ("mean", "low", "high"))
    return f"{m:+.{digits}f} [{lo:+.{digits}f}, {hi:+.{digits}f}]"


TITLES = {"every": "Every game", "rested": "Rested games", "back_to_back": "Back-to-back games"}


def markdown(found: Mapping[str, Any], version: str) -> str:
    """The report."""
    w = found["weights"]
    lines = [
        f"# close_forecast version 2 on the training seasons: {version}",
        "",
        "ADR 0056. Version 1's split regression (ADR 0054) with #370's five schedule density",
        "columns beside B3's four parts. SBR's opener and close, de-vigged multiplicatively, on",
        "the yardstick's rows (ADR 0053). Skill is each forecast's squared error less no move's,",
        "in thousandths of squared log-odds; negative means it beats no move, and a negative",
        "difference favours version 2. Intervals are 95% weekly block bootstrap. The features'",
        "settings were tuned on these seasons (ADR 0011), and #370 chose density on 2013-14 and",
        "2014-15: development evidence only.",
        "",
        "## 1. The frozen fit",
        "",
        f"On {found['frozen_games']:,} games from 2012-13 to 2017-18, train_cutoff "
        f"{found['train_cutoff']}.",
        "",
        "| Weight | Value |",
        "| --- | --- |",
        *(f"| {name} | {w[name]:+.4f} |" for name in WEIGHTS),
        "",
        "## 2. Out of sample, beside version 1",
        "",
        "Each season from 2013-14 fitted on the earlier ones from 2012-13, both versions on the",
        "same games.",
        "",
        "| Games | Count | Version 1 | Version 2 | Version 2 less version 1 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name in SUBSETS:
        row = found["oos"][name]
        lines.append(
            f"| {TITLES[name]} | {row['games']:,} | {_cell(row['s_1'], 1000)} "
            f"| {_cell(row['s_2'], 1000)} | {_cell(row['difference'], 1000)} |"
        )
    rested = found["oos"]["rested"]["difference"]
    verdict = (
        "**The stop holds:** version 2 less version 1 is not below 0 on the rested games, so "
        "the build stops and returns to the owner (ADR 0056)."
        if found["stop"]
        else "**The build goes ahead:** version 2 less version 1 is below 0 on the rested games, "
        f"{_cell(rested, 1000)} (ADR 0056's stop)."
    )
    lines += ["", verdict, "", "## 3. The forecast moves", ""]
    lines += ["| Version | Mean size, points | At 1 point or more | At 2 points or more |"]
    lines += ["| --- | --- | --- | --- |"]
    for v in VERSIONS:
        s = found["sizes"][v]
        lines.append(f"| {v} | {s['mean'] * 100:.2f} | {s['at_1']:.0%} | {s['at_2']:.0%} |")
    lines += [
        "",
        "## 4. Taking at the opener",
        "",
        f"The opener's median margin is {found['margin']:.1%}. A side is taken where its forecast",
        "close times its opener price, less 1, is above the threshold (hard rule 4), and valued",
        "at the de-vigged close. Setting aside the moves above 8 points reads the close: a",
        "diagnosis, not a rule. SBR's opener may predate the previous night's result (ADR 0006),",
        "so the rested games are the clean ones.",
        "",
        "| Version | Games | Expected return above | Sides | CLV | Without the moves above 8 "
        "points (diagnosis) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in found["takes"]:
        lines.append(
            f"| {row['version']} | {TITLES[row['subset']]} | {row['threshold']:.0%} "
            f"| {row['sides']:,} | {_cell(row['clv'], 100, 2)}% "
            f"| {row['kept']:,}: {_cell(row['clv_kept'], 100, 2)}% |"
        )
    return "\n".join(lines) + "\n"


def run(frame: pl.DataFrame, prices: pl.DataFrame) -> dict[str, Any]:
    """Every figure of the report from the rows with density (with_density) and the openers'
    prices (opener_prices)."""
    fitted = frame.filter(pl.col("season") >= ys.FIRST_FIT)
    seasons = tuple(s for s in sorted(fitted["season"].unique().to_list()) if s > ys.FIRST_FIT)
    games = out_of_sample(frame, seasons).join(
        frame.select("game_id", "o", "p_close"), on="game_id"
    )
    found: dict[str, Any] = {
        "weights": frozen_fit(fitted),
        "frozen_games": fitted.height,
        "train_cutoff": fitted["result_utc"].max(),
        "oos": {},
        "sizes": {
            v: sizes(games["o"].to_numpy(), games[f"y_hat_{v}"].to_numpy()) for v in VERSIONS
        },
        "takes": [],
    }
    for name in SUBSETS:
        of = subset(games, name)
        found["oos"][name] = {"games": of.height} | {
            column: metrics.bootstrap(of, column).to_dict()
            for column in ("s_1", "s_2", "difference")
        }
    found["stop"] = stop(found["oos"]["rested"]["difference"])
    margin = games.join(prices, on="game_id").select(1 / pl.col("home") + 1 / pl.col("away") - 1)
    found["margin"] = float(margin.to_series().median())  # type: ignore[arg-type]
    for v in VERSIONS:
        priced = games.with_columns(y_hat=pl.col(f"y_hat_{v}"))
        for name in ("every", "rested"):
            for threshold in THRESHOLDS:
                taken = takes(subset(priced, name), prices, threshold)
                kept = taken.filter(pl.col("moved") <= REVIEW)
                found["takes"].append(
                    {
                        "version": v,
                        "subset": name,
                        "threshold": threshold,
                        "sides": taken.height,
                        "clv": metrics.bootstrap(taken, "clv").to_dict(),
                        "kept": kept.height,
                        "clv_kept": metrics.bootstrap(kept, "clv").to_dict(),
                    }
                )
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description="close_forecast version 2 on history (ADR 0056).")
    parser.add_argument("--out", type=Path, default=Path("reports/yardstick"))
    parser.add_argument(
        "--artifact",
        type=Path,
        default=None,
        help="Also write the frozen fit, version 2's weights (ADR 0056), as "
        "close-forecast-2-<yyyymmdd>-<shortsha>.json in this directory (reports/live).",
    )
    args = parser.parse_args()
    lake = Lake()
    frame = with_density(ys.read(lake, max(ys.CONFIRM)))
    found = run(frame, opener_prices(lake, max(ys.CONFIRM)))
    now = datetime.now(UTC)
    version = reports.version("close-forecast-2-history", now)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{version}.json").write_text(
        json.dumps(found, indent=2, sort_keys=True, default=str)
    )
    path = args.out / f"{version}.md"
    path.write_text(markdown(found, version))
    print(f"wrote {path}")
    print("the stop holds: the build returns to the owner" if found["stop"] else "build goes ahead")
    if args.artifact is not None:
        fitted = frame.filter(pl.col("season") >= ys.FIRST_FIT)
        written = write_artifact(
            args.artifact, found["weights"], fitted, reports.version("close-forecast-2", now)
        )
        print(f"wrote {written}")


if __name__ == "__main__":
    main()
