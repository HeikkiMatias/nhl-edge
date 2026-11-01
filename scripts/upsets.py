"""#366: in mismatches, does the weaker team win more often than the price says, and are there
letdown spots where a strong team plays down to a weak one? On the yardstick's explore half
(2013-14 and 2014-15) only, with ADR 0053's yardstick; the confirm half stays unread.

    uv run python scripts/upsets.py

Fixed before any reading (#366):
1. **Calibration in mismatches.** Per band of the favourite's de-vigged probability (BANDS), its
   realised win rate on the full game (overtime and shootout included, hard rule 2) less its
   probability, game by game, with weekly block bootstrap intervals. For the close and the opener
   (SBR, de-vigged under B1_METHOD), and for a model-side probability: a logistic fit of the home
   win on B3's expected goal difference and the home edge, per season on the earlier seasons
   from 2012-13 (B3's own fit refuses these seasons, ADR 0011). Negative means favourites win
   less often than priced: upsets more common than the price says.
2. **Letdown spots** in mismatches, the favourite by the opener at MISMATCH or more, each from
   results public before the opener's assumed 10:00 ET:
   - **big win:** the favourite won its previous game by BIG goals or more;
   - **win streak:** the favourite has won its last STREAK games;
   - **slumping underdog:** the underdog has lost its last STREAK games;
   - **road trip:** the game is the favourite's ROAD-th straight away game or later.
   Each spot gets: the favourite's win rate less its closing probability on the flagged games, less
   the same on the other mismatches; and, as a yardstick feature (ADR 0053), its move skill and its
   result beyond the close. The feature is -1 where the home team is a favourite in the spot, +1
   where the away team is, and 0 elsewhere.
   A look-ahead spot (the favourite plays again the next day) is left out: under ADR 0005 another
   game shows only once its result is public, since its presence would say it wasn't postponed.

Many looks: a finding is exploratory, and one the owner wants kept becomes a claim scored once on
the confirm half (ADR 0053), as development evidence only after the first confirm run.

It reads the lake as `nhl research yardstick` does and writes
reports/yardstick/upsets-<yyyymmdd>-<shortsha>.md. It writes nothing to the lake.
"""

import argparse
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from sklearn.linear_model import LogisticRegression

from nhl_edge.backtest import metrics, reports
from nhl_edge.backtest import yardstick as ys
from nhl_edge.lake.tables import Lake

BANDS = ((0.60, 0.65), (0.65, 0.70), (0.70, 1.0))
MISMATCH = 0.60
BIG = 3
STREAK = 4
ROAD = 4
SPOTS = ("big win", "win streak", "slumping underdog", "road trip")


def history(games: pl.DataFrame) -> pl.DataFrame:
    """Each team-game with its result on the full game, when it was public, and the runs ending
    at it within its season: wins, losses and away games in a row."""
    sides = []
    for side, other, home in (("home", "away", True), ("away", "home", False)):
        sides.append(
            games.select(
                "season",
                "start_utc",
                team=side,
                is_home=pl.lit(home),
                margin=(pl.col(f"{side}_score").cast(pl.Int32) - pl.col(f"{other}_score")),
                result_utc="observed_utc",
            )
        )
    team_games = pl.concat(sides).sort("start_utc").with_columns(won=pl.col("margin") > 0)
    team = ("season", "team")
    for name, flag in (
        ("wins", pl.col("won")),
        ("losses", ~pl.col("won")),
        ("away", ~pl.col("is_home")),
    ):
        # The length of the run of flag ending at each game, 0 where flag is false: a run starts
        # wherever flag differs from the team's game before.
        starts = (flag != flag.shift(1)).fill_null(True).cast(pl.Int32).cum_sum().over(team)
        team_games = team_games.with_columns(run=starts).with_columns(
            pl.when(flag)
            .then(pl.int_range(1, pl.len() + 1).over(*team, "run"))
            .otherwise(0)
            .alias(name)
        )
    return team_games.drop("run")


def form(frame: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """Each game of frame with each side's form before its opener: its previous game's margin, its
    wins and losses in a row, and its away games in a row counting this one. From the team-games
    whose results were public before the game's prediction time, in the same season."""
    played = history(games).select(
        "season", "team", "result_utc", "margin", "wins", "losses", "away"
    )
    targets = frame.select("game_id", "season", "prediction_utc").join(
        games.select("game_id", "home", "away"), on="game_id"
    )
    out = targets.select("game_id")
    for side in ("home", "away"):
        known = (
            targets.select("game_id", "season", "prediction_utc", team=side)
            .sort("prediction_utc")
            .join_asof(
                played.sort("result_utc"),
                left_on="prediction_utc",
                right_on="result_utc",
                by=["season", "team"],
                strategy="backward",
                allow_exact_matches=False,
            )
            .select(
                "game_id",
                pl.col("margin").alias(f"{side}_margin"),
                pl.col("wins").fill_null(0).alias(f"{side}_wins"),
                pl.col("losses").fill_null(0).alias(f"{side}_losses"),
                # This game counts toward an away run only for the away side.
                (pl.col("away").fill_null(0) + 1 if side == "away" else pl.lit(0)).alias(
                    f"{side}_road"
                ),
            )
        )
        out = out.join(known, on="game_id")
    return out


def spots(frame: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """Each game of frame with the favourite by the opener (home_favourite), the favourite's
    opening and closing probabilities and whether it won, and a flag per spot (SPOTS), true only
    in mismatches (the favourite at MISMATCH or more at the opener)."""
    p_open, p_close = pl.col("p_open"), pl.col("p_close")
    home = pl.col("home_favourite")

    def fav(column: str) -> pl.Expr:
        return pl.when(home).then(pl.col(f"home_{column}")).otherwise(pl.col(f"away_{column}"))

    def dog(column: str) -> pl.Expr:
        return pl.when(home).then(pl.col(f"away_{column}")).otherwise(pl.col(f"home_{column}"))

    mismatch = pl.col("fav_open") >= MISMATCH
    big, streak, slump, trip = SPOTS
    return (
        frame.select("game_id", "season", "game_date", "p_open", "p_close", "home_win")
        .join(form(frame, games), on="game_id")
        .with_columns(home_favourite=p_open >= 0.5)
        .with_columns(
            fav_open=pl.when(home).then(p_open).otherwise(1 - p_open),
            fav_close=pl.when(home).then(p_close).otherwise(1 - p_close),
            fav_won=pl.when(home).then(pl.col("home_win")).otherwise(1 - pl.col("home_win")),
        )
        .with_columns(
            (mismatch & (fav("margin").fill_null(0) >= BIG)).alias(big),
            (mismatch & (fav("wins") >= STREAK)).alias(streak),
            (mismatch & (dog("losses") >= STREAK)).alias(slump),
            (mismatch & (fav("road") >= ROAD)).alias(trip),
        )
        .select(
            "game_id",
            "season",
            "game_date",
            "home_favourite",
            "fav_open",
            "fav_close",
            "fav_won",
            *SPOTS,
        )
    )


def model_side(frame: pl.DataFrame, seasons: Sequence[int]) -> pl.DataFrame:
    """Each game of the seasons with p_model: a logistic fit of the home win on B3's Δĝ and the
    home edge, per season on the earlier seasons' games from FIRST_FIT whose results were public
    before its fold start (B3's own fit refuses these seasons, ADR 0011)."""
    columns = ["delta_g_hat", "h_s"]
    out = []
    for season in sorted(seasons):
        start = ys.fold_start(frame, season)
        train = frame.filter(
            pl.col("season").is_between(ys.FIRST_FIT, season - 1), pl.col("result_utc") < start
        )
        test = frame.filter(pl.col("season") == season)
        if train["home_win"].n_unique() < 2:
            raise ValueError(f"{season}: no results from {ys.FIRST_FIT} on to fit the model on")
        # Unpenalized, as ADR 0011's win model.
        model = LogisticRegression(C=np.inf, max_iter=1000)
        model.fit(train.select(columns).to_numpy(), train["home_win"].to_numpy())
        p = model.predict_proba(test.select(columns).to_numpy())[:, 1]
        out.append(test.select("game_id").with_columns(p_model=pl.Series(p)))
    return pl.concat(out)


def calibration(rows: pl.DataFrame, probability: str) -> dict[str, dict[str, Any]]:
    """Per band of the favourite's probability (probability, home side's), its games, mean
    probability, win rate, and win rate less probability with its weekly block bootstrap
    interval."""
    fav = rows.with_columns(
        p=pl.when(pl.col(probability) >= 0.5)
        .then(pl.col(probability))
        .otherwise(1 - pl.col(probability)),
        won=pl.when(pl.col(probability) >= 0.5)
        .then(pl.col("home_win"))
        .otherwise(1 - pl.col("home_win")),
    ).with_columns(gap=pl.col("won").cast(pl.Float64) - pl.col("p"))
    found: dict[str, dict[str, Any]] = {}
    for low, high in ((0.5, 1.0), *BANDS):
        band = fav.filter(pl.col("p") >= low, pl.col("p") < high if high < 1 else pl.lit(True))
        label = "every favourite" if low == 0.5 else f"{low:.2f} to {high:.2f}"
        if band.is_empty():
            found[label] = {"games": 0, "p": None, "won": None, "gap": None}
            continue
        found[label] = {
            "games": band.height,
            "p": float(band["p"].mean()),  # type: ignore[arg-type]
            "won": float(band["won"].mean()),  # type: ignore[arg-type]
            "gap": metrics.bootstrap(band, "gap").to_dict(),
        }
    return found


def letdowns(frame: pl.DataFrame, flags: pl.DataFrame) -> dict[str, dict[str, Any]]:
    """Per spot: its mismatches' count, the favourite's win rate less its closing probability on
    them less that on the other mismatches, and as a yardstick feature its move skill (on every
    game and on the rested ones) and its result beyond the close."""
    mismatches = flags.filter(pl.col("fav_open") >= MISMATCH).with_columns(
        gap=pl.col("fav_won").cast(pl.Float64) - pl.col("fav_close")
    )
    found: dict[str, dict[str, Any]] = {}
    for spot in SPOTS:
        sign = pl.when(pl.col("home_favourite")).then(-1.0).otherwise(1.0)
        feature = flags.select(
            "game_id", pl.when(pl.col(spot)).then(sign).otherwise(0.0).alias("x")
        )
        with_x = frame.join(feature, on="game_id")
        games = ys.scored(with_x, ("x",), ys.EXPLORE)
        inside = mismatches.filter(pl.col(spot), pl.col("season").is_in(list(ys.EXPLORE)))
        outside = mismatches.filter(~pl.col(spot), pl.col("season").is_in(list(ys.EXPLORE)))
        found[spot] = {
            "games": inside.height,
            "gap": metrics.bootstrap(inside, "gap").to_dict() if inside.height else None,
            "gap_less_others": (
                metrics.difference(inside, outside, "gap").to_dict() if inside.height else None
            ),
            "move": metrics.bootstrap(games, "move").to_dict(),
            # On the rested games, where SBR's opener can't predate either team's last result
            # (ADR 0006), as the yardstick's claims are read.
            "move_rested": metrics.bootstrap(games.filter(pl.col("rested")), "move").to_dict(),
            "result": metrics.bootstrap(games, "result").to_dict(),
        }
    return found


def _cell(estimate: Mapping[str, Any] | None, scale: float = 1.0, key: str = "mean") -> str:
    if estimate is None:
        return ""
    return (
        f"{estimate[key] * scale:+.3f} [{estimate['low'] * scale:+.3f}, "
        f"{estimate['high'] * scale:+.3f}]"
    )


def markdown(
    bands: Mapping[str, Mapping[str, Any]], spots_: Mapping[str, Any], version: str
) -> str:
    """The report: calibration in mismatches, then the letdown spots."""
    lines = [
        f"# Upsets in mismatches: explore, {version}",
        "",
        "#366, on the yardstick's explore half (2013-14 and 2014-15) only. Win rates are on the",
        "full game, overtime and shootout included. Intervals are 95% weekly block bootstrap.",
        "Many looks: a finding is exploratory, a claim for the confirm half only if the owner",
        "keeps it (ADR 0053).",
        "",
        "## Calibration: do favourites win as often as priced?",
        "",
        "Per band of the favourite's probability: its win rate less its probability, game by game.",
        "Negative means favourites win less often than priced (upsets more common). The model",
        "side's features are in-sample in their settings, tuned on these seasons (ADR 0011).",
        "",
    ]
    for name, found in bands.items():
        lines += [
            f"### {name}",
            "",
            "| Favourite's probability | Games | Mean probability | Win rate | Win rate less "
            "probability |",
            "| --- | --- | --- | --- | --- |",
        ]
        for label, row in found.items():
            if not row["games"]:
                lines.append(f"| {label} | 0 | | | |")
                continue
            lines.append(
                f"| {label} | {row['games']:,} | {row['p']:.3f} | {row['won']:.3f} "
                f"| {_cell(row['gap'])} |"
            )
        lines.append("")
    lines += [
        "## Letdown spots in mismatches",
        "",
        f"The favourite by the opener at {MISMATCH:.2f} or more. The calibration gap is the",
        "favourite's win rate less its closing probability; negative means it loses more often",
        "than the close says. The yardstick's move skill and result beyond the close are in",
        "thousandths; negative means the spot anticipates the move or adds to the close. The",
        "rested games are those where neither team played the day before (ADR 0006).",
        "",
        "| Spot | Games | Gap | Gap less other mismatches | Move skill | Move skill, rested "
        "| Beyond the close |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for spot, row in spots_.items():
        lines.append(
            f"| {spot} | {row['games']:,} | {_cell(row['gap'])} "
            f"| {_cell(row['gap_less_others'], key='value')} | {_cell(row['move'], 1000)} "
            f"| {_cell(row['move_rested'], 1000)} | {_cell(row['result'], 1000)} |"
        )
    lines += [
        "",
        "A look-ahead spot (the favourite plays again the next day) is left out: under ADR 0005",
        "another game shows only once its result is public.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Upsets in mismatches (#366).")
    parser.add_argument("--out", type=Path, default=Path("reports/yardstick"))
    args = parser.parse_args()
    read = ys.read(Lake(), max(ys.EXPLORE))
    frame = read.frame
    explore = frame.filter(pl.col("season").is_in(list(ys.EXPLORE)))
    model = explore.join(model_side(frame, ys.EXPLORE), on="game_id")
    bands = {
        "The close": calibration(explore, "p_close"),
        "The opener": calibration(explore, "p_open"),
        "The model side (Δĝ and the home edge)": calibration(model, "p_model"),
    }
    flags = spots(frame, read.games)
    version = reports.version("upsets", datetime.now(UTC))
    report = markdown(bands, letdowns(frame, flags), version)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{version}.md"
    path.write_text(report)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
