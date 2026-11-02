"""#369: the close forecaster's evidence on the training seasons, for its ADR (0054). It reads the
yardstick's rows (ADR 0053) to 2017-18 and writes
reports/yardstick/close-forecast-history-<yyyymmdd>-<shortsha>.md. It writes nothing to the lake.

    uv run python scripts/close_forecast_history.py

The forecast is the yardstick's split regression: the move from SBR's opener to its close, in the
home side's log-odds, y_hat = a + b_o*o + b*parts, with o the opener's log-odds and parts B3's four
parts of the expected goal difference as known at the opener (ADR 0044). Four readings:
1. **The frozen fit:** its weights on every game from 2012-13 to 2017-18, the fit the live
   forecaster would carry, with its in-sample move skill against no move.
2. **Out of sample:** each season from 2013-14 fitted on the earlier ones (ys.scored), with the
   size of the forecast moves in probability points.
3. **A simpler form, against it:** the move as k of the way from the opener toward an outcome
   model's log-odds, the model a logistic fit of the home win on B3's whole difference and the
   home edge, as live B3 weighs them, each on the earlier seasons. It is what the morning view's
   stored disagreement with B3 could forecast alone. Scored on the games both forecast, as a
   paired difference.
4. **Taking at the opener:** a side is taken where p_hat_close * its opener price - 1 > 0 (hard
   rule 4, the executable price), and valued at the de-vigged close (CLV). Beside it, the same
   without the games whose price moved more than REVIEW points from the opener to the close: a
   diagnosis that reads the close, never a rule a bettor could follow.

Every interval is a weekly block bootstrap (hard rule 7). The features' settings were tuned on
these seasons (ADR 0011), so the figures are development evidence; the confirmation is live.
"""

import argparse
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray
from sklearn.linear_model import LogisticRegression

from nhl_edge.backtest import metrics, reports
from nhl_edge.backtest import yardstick as ys
from nhl_edge.game import b3
from nhl_edge.lake.tables import Lake

PARTS = b3.SPLIT
WEIGHTS = ("a", "b_o", *PARTS)
# Taking is read at expected returns above these (hard rule 4).
THRESHOLDS = (0.0, 0.01)
# The opener-to-close move, in probability points, above which a game is set aside in the
# diagnosis (hard rule 8's review size).
REVIEW = 0.08
# The simpler form's outcome model: B3's whole difference and the home edge, as live B3 weighs
# them, so its gap stands for the morning view's stored disagreement with B3.
OUTCOME_INPUTS = ("delta_g_hat", "h_s")


def _logistic(z: NDArray[np.float64]) -> NDArray[np.float64]:
    return 1 / (1 + np.exp(-z))


def design(rows: pl.DataFrame) -> NDArray[np.float64]:
    """The fit's columns: 1, o and the four parts."""
    return np.column_stack(
        [np.ones(rows.height), rows["o"].to_numpy(), *(rows[p].to_numpy() for p in PARTS)]
    ).astype(np.float64)


def frozen_fit(rows: pl.DataFrame) -> dict[str, float]:
    """The split regression's weights by least squares on every row, as ys.scored fits a season."""
    beta, *_ = np.linalg.lstsq(design(rows), rows["y"].to_numpy(), rcond=None)
    return dict(zip(WEIGHTS, (float(b) for b in beta), strict=True))


def forecast(rows: pl.DataFrame, weights: Mapping[str, float]) -> NDArray[np.float64]:
    """The forecast move y_hat for each row."""
    return design(rows) @ np.array([weights[w] for w in WEIGHTS])


def sizes(o: NDArray[np.float64], y_hat: NDArray[np.float64]) -> dict[str, float]:
    """The forecast moves in probability points of the home side: their mean size, and the share
    of games at 1 and 2 points or more."""
    points = np.abs(_logistic(o + y_hat) - _logistic(o))
    return {
        "mean": float(points.mean()),
        "at_1": float(np.mean(points >= 0.01)),
        "at_2": float(np.mean(points >= 0.02)),
    }


def against_no_move(games: pl.DataFrame) -> pl.DataFrame:
    """ys.scored's games with s, each forecast's squared error less that of no move: the live
    forecaster's measure (ADR 0049's), where the yardstick's move skill is against the opener's
    own drift."""
    return games.with_columns(s=(pl.col("y") - pl.col("y_hat")) ** 2 - pl.col("y") ** 2)


def shrink(frame: pl.DataFrame, seasons: Sequence[int]) -> pl.DataFrame:
    """The simpler form's per-game skill against no move (s_shrink), beside the split's (s_split)
    on the same games. The outcome model is fitted per season on the earlier seasons from
    FIRST_FIT on results public before the fold start; k per season on the earlier seasons'
    model gaps, so a season needs two earlier ones."""
    gaps = []
    for season in sorted({s for s in frame["season"].to_list() if s > ys.FIRST_FIT}):
        start = ys.fold_start(frame, season)
        train = frame.filter(
            pl.col("season").is_between(ys.FIRST_FIT, season - 1), pl.col("result_utc") < start
        )
        test = frame.filter(pl.col("season") == season)
        model = LogisticRegression(C=np.inf, max_iter=2000)
        model.fit(train.select(OUTCOME_INPUTS).to_numpy(), train["home_win"].to_numpy())
        z = model.decision_function(test.select(OUTCOME_INPUTS).to_numpy())
        gaps.append(test.select("game_id").with_columns(d=pl.Series(z) - test["o"]))
    with_gap = frame.join(pl.concat(gaps), on="game_id")
    split = against_no_move(ys.scored(frame, PARTS, seasons)).select("game_id", s_split="s")
    out = []
    for season in seasons:
        start = ys.fold_start(with_gap, season)
        train = with_gap.filter(
            pl.col("season").is_between(ys.FIRST_FIT + 10_001, season - 1),
            pl.col("result_utc") < start,
        )
        if train.is_empty():
            raise ValueError(f"{season}: no earlier season with an out-of-sample model gap")
        x, y = train["d"].to_numpy(), train["y"].to_numpy()
        k = float(x @ y / (x @ x))
        test = with_gap.filter(pl.col("season") == season)
        y_hat = k * test["d"].to_numpy()
        out.append(
            test.select("game_id", "season", "game_date", "rested", "y").with_columns(
                s_shrink=(pl.col("y") - pl.Series(y_hat)) ** 2 - pl.col("y") ** 2, k=pl.lit(k)
            )
        )
    return pl.concat(out).join(split, on="game_id")


def takes(games: pl.DataFrame, prices: pl.DataFrame, threshold: float) -> pl.DataFrame:
    """Each side whose expected return at its opener price, on the forecast close, is above
    threshold: its expected return (ev), its CLV at the de-vigged close, and the game's move in
    probability points. games holds o, y_hat and p_close; prices the opener's decimal prices per
    game (home, away)."""
    rows = games.join(prices, on="game_id").with_columns(
        p_hat=1 / (1 + (-(pl.col("o") + pl.col("y_hat"))).exp()),
        moved=(pl.col("p_close") - 1 / (1 + (-pl.col("o")).exp())).abs(),
    )
    sides = [
        rows.select(
            "game_id",
            "season",
            "game_date",
            "moved",
            ev=p * pl.col(side) - 1,
            clv=close * pl.col(side) - 1,
        )
        for side, p, close in (
            ("home", pl.col("p_hat"), pl.col("p_close")),
            ("away", 1 - pl.col("p_hat"), 1 - pl.col("p_close")),
        )
    ]
    return pl.concat(sides).filter(pl.col("ev") > threshold)


def opener_prices(lake: Lake, last: int) -> pl.DataFrame:
    """SBR's opener per game of the seasons to last: its home and away decimal prices."""
    openers = lake.read("sbr_odds").filter(
        pl.col("season") <= last, pl.col("market") == "h2h", pl.col("quote") == "open"
    )
    return openers.pivot(on="side", index="game_id", values="price_decimal").select(
        "game_id", "home", "away"
    )


def _cell(estimate: Mapping[str, Any], scale: float = 1.0, digits: int = 3) -> str:
    m, lo, hi = (estimate[k] * scale for k in ("mean", "low", "high"))
    return f"{m:+.{digits}f} [{lo:+.{digits}f}, {hi:+.{digits}f}]"


def markdown(found: Mapping[str, Any], version: str) -> str:
    """The report."""
    w = found["weights"]
    lines = [
        f"# The close forecaster on the training seasons: {version}",
        "",
        "#369, for ADR 0054. SBR's opener and close, de-vigged multiplicatively, on the",
        "yardstick's rows (ADR 0053). Move skill is in thousandths of squared log-odds against",
        "no move; negative means the forecast beats it. Intervals are 95% weekly block",
        "bootstrap. The features' settings were tuned on these seasons (ADR 0011): development",
        "evidence only.",
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
        f"In sample, its move skill is {_cell(found['frozen_skill'], 1000)}, against a no-move",
        f"error of {found['frozen_baseline'] * 1000:.3f}.",
        "",
        "## 2. Out of sample",
        "",
        "Each season fitted on the earlier ones from 2012-13.",
        "",
        "| Seasons | Games | Move skill | Rested games | Against the opener's drift |",
        "| --- | --- | --- | --- | --- |",
        f"| 2013-14 to 2017-18 | {found['oos_games']:,} | {_cell(found['oos_skill'], 1000)} "
        f"| {_cell(found['oos_rested'], 1000)} | {_cell(found['oos_drift'], 1000)} |",
        "",
        f"The forecast moves average {found['sizes']['mean'] * 100:.2f} probability points. "
        f"{found['sizes']['at_1']:.0%} of games",
        f"have one of 1 point or more, and {found['sizes']['at_2']:.0%} of 2 points or more.",
        "",
        "## 3. A simpler form: k of the way toward B3's whole difference",
        "",
        "| Seasons | Games | k by season | Its move skill | Split's on the same games "
        "| Simpler less split |",
        "| --- | --- | --- | --- | --- | --- |",
        f"| 2014-15 to 2017-18 | {found['shrink_games']:,} | {found['shrink_k']} "
        f"| {_cell(found['shrink_skill'], 1000)} | {_cell(found['shrink_split'], 1000)} "
        f"| {_cell(found['shrink_less_split'], 1000)} |",
        "",
        "## 4. Taking at the opener",
        "",
        f"The opener's median margin is {found['margin']:.1%}. Each taken side is valued at the",
        "de-vigged close.",
        "",
        "| Expected return above | Sides taken | CLV | Without the moves above 8 points "
        "(diagnosis) |",
        "| --- | --- | --- | --- |",
    ]
    for row in found["takes"]:
        lines.append(
            f"| {row['threshold']:.0%} | {row['sides']:,} | {_cell(row['clv'], 100, 2)}% "
            f"| {row['kept']:,}: {_cell(row['clv_kept'], 100, 2)}% |"
        )
    lines += [
        "",
        f"{found['review_games']} games moved more than 8 points from the opener to the close.",
        "Setting them aside reads the close, so it is a diagnosis, not a rule.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="The close forecaster on history (#369).")
    parser.add_argument("--out", type=Path, default=Path("reports/yardstick"))
    args = parser.parse_args()
    lake = Lake()
    frame = ys.read(lake, max(ys.CONFIRM)).frame.drop_nulls(["y", "o", *PARTS, "h_s"])
    fitted = frame.filter(pl.col("season") >= ys.FIRST_FIT)
    weights = frozen_fit(fitted)
    y = fitted["y"].to_numpy()
    skill = fitted.with_columns(
        s=(pl.col("y") - pl.Series(forecast(fitted, weights))) ** 2 - pl.col("y") ** 2
    )
    seasons = tuple(s for s in sorted(fitted["season"].unique().to_list()) if s > ys.FIRST_FIT)
    games = against_no_move(ys.scored(frame, PARTS, seasons)).join(
        frame.select("game_id", "o", "p_close"), on="game_id"
    )
    compared = shrink(frame, seasons[1:])
    prices = opener_prices(lake, max(ys.CONFIRM))
    margin = games.join(prices, on="game_id").select(1 / pl.col("home") + 1 / pl.col("away") - 1)
    found: dict[str, Any] = {
        "weights": weights,
        "frozen_games": fitted.height,
        "train_cutoff": fitted["result_utc"].max(),
        "frozen_skill": metrics.bootstrap(skill, "s").to_dict(),
        "frozen_baseline": float(np.mean(y**2)),
        "oos_games": games.height,
        "oos_skill": metrics.bootstrap(games, "s").to_dict(),
        "oos_rested": metrics.bootstrap(games.filter(pl.col("rested")), "s").to_dict(),
        "oos_drift": metrics.bootstrap(games, "move").to_dict(),
        "sizes": sizes(games["o"].to_numpy(), games["y_hat"].to_numpy()),
        "shrink_games": compared.height,
        "shrink_k": ", ".join(
            f"{k:.3f}"
            for k in compared.group_by("season")
            .agg(pl.col("k").first())
            .sort("season")["k"]
            .to_list()
        ),
        "shrink_skill": metrics.bootstrap(compared, "s_shrink").to_dict(),
        "shrink_split": metrics.bootstrap(compared, "s_split").to_dict(),
        "shrink_less_split": metrics.bootstrap(
            compared.with_columns(d=pl.col("s_shrink") - pl.col("s_split")), "d"
        ).to_dict(),
        "margin": float(margin.to_series().median()),  # type: ignore[arg-type]
        "review_games": games.filter(
            (pl.col("p_close") - 1 / (1 + (-pl.col("o")).exp())).abs() > REVIEW
        ).height,
        "takes": [],
    }
    for threshold in THRESHOLDS:
        taken = takes(games, prices, threshold)
        kept = taken.filter(pl.col("moved") <= REVIEW)
        found["takes"].append(
            {
                "threshold": threshold,
                "sides": taken.height,
                "clv": metrics.bootstrap(taken, "clv").to_dict(),
                "kept": kept.height,
                "clv_kept": metrics.bootstrap(kept, "clv").to_dict(),
            }
        )
    version = reports.version("close-forecast-history", datetime.now(UTC))
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{version}.md"
    path.write_text(markdown(found, version))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
