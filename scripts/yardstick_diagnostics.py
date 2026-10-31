"""Step 2 of #361 (ADR 0053): where and what the features know before the market, on the
yardstick's explore half (2012-13 to 2014-15) only. The confirm half stays unread.

    uv run python scripts/yardstick_diagnostics.py

Fixed before any subset was read:
1. **What each part adds:** the move skill of every feature together (`all`) less that of `all`
   without one of its parts, game by game on the games both scored. Negative means the part adds
   to the forecast of the move beyond the others.
2. **Where the market learns late:** the move skill of B2's inputs, B3's split and all of them on
   each subset of the games and its complement, the split less B2's inputs on the games both
   scored (a player-level model against a team-level one), and the squared move itself, which
   shows where the market moves most:
   - **early season:** both teams within their first EARLY games of the season, before the
     market has learned the offseason's rosters;
   - **back-to-back:** either team played the day before (the rested games' complement);
   - **long travel:** the travel difference in the top quarter of the half's games;
   - **results ahead of xG:** the gap between the teams' season-to-date goal difference and B3's
     expected one, per game, from the results public before the opener, in the top quarter;
   - **goalie surprise:** either team's starter was not its likeliest candidate at the opener.
     The starter is known only from the boxscore, so this describes where the market moves; it
     is no feature.

Every figure has its weekly block bootstrap interval (hard rule 7). With a dozen subsets, some
intervals will exclude zero by chance: a finding becomes a claim, written down and scored once on
the confirm half (ADR 0053), before anything rests on it.

It reads the lake as `nhl research yardstick` does (`Lake.pull` brings the tables from R2) and
writes reports/yardstick/diagnostics-<version>.md. It writes nothing to the lake.
"""

import argparse
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from nhl_edge.backtest import metrics, reports
from nhl_edge.backtest import yardstick as ys
from nhl_edge.game import b2
from nhl_edge.lake.tables import Lake
from nhl_edge.lineup.goalie_start import team_goalie_games

EARLY = 20
TOP = 0.75
SETS = ("B2 inputs", "B3 split", "all")
SUBSETS = {
    "early season": "both teams within their first 20 games",
    "back-to-back": "either team played the day before",
    "long travel": "travel difference in the top quarter",
    "results ahead of xG": "season-to-date goals less expected, top quarter of the gap",
    "goalie surprise": "either starter not its team's likeliest at the opener",
}


def candidates() -> dict[str, tuple[str, ...]]:
    """B2's inputs, B3's split and all of them, and all of them less each part."""
    every = ys.CANDIDATES["all"]
    found = {name: ys.CANDIDATES[name] for name in SETS}
    for part in every:
        found[f"all less {part}"] = tuple(name for name in every if name != part)
    return found


def game_numbers(games: pl.DataFrame) -> pl.DataFrame:
    """Each game with each team's game number in its season, by start (home_n, away_n)."""
    sides = pl.concat(
        [games.select("game_id", "season", "start_utc", team=side) for side in ("home", "away")]
    )
    numbered = sides.with_columns(n=pl.col("start_utc").rank("ordinal").over("season", "team"))
    out = games.select("game_id", "home", "away")
    for side in ("home", "away"):
        out = out.join(
            numbered.select("game_id", side=pl.col("team"), n="n").rename(
                {"side": side, "n": f"{side}_n"}
            ),
            on=["game_id", side],
        )
    return out.select("game_id", "home_n", "away_n")


def luck_gaps(frame: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """Each game of frame with gap: the home team's season-to-date goal difference per game less
    B3's expected one (Δĝ), over its earlier games of frame whose results were public before the
    game's opener, less the away team's. 0 for a team without one."""
    played = frame.join(
        games.select("game_id", "home", "away", "home_score", "away_score"), on="game_id"
    ).with_columns(diff=(pl.col("home_score") - pl.col("away_score")) - pl.col("delta_g_hat"))
    teams = pl.concat(
        [
            played.select("season", "result_utc", team="home", luck="diff"),
            played.select("season", "result_utc", team="away", luck=-pl.col("diff")),
        ]
    ).sort("result_utc")
    running = teams.with_columns(
        total=pl.col("luck").cum_sum().over("season", "team"),
        n=pl.int_range(1, pl.len() + 1).over("season", "team"),
    )
    targets = played.select("game_id", "season", "home", "away", "prediction_utc")
    out = targets
    for side in ("home", "away"):
        known = (
            targets.select("game_id", "season", "prediction_utc", team=side)
            .sort("prediction_utc")
            .join_asof(
                running.select("season", "team", "result_utc", "total", "n"),
                left_on="prediction_utc",
                right_on="result_utc",
                by=["season", "team"],
                strategy="backward",
                allow_exact_matches=False,
            )
            .select("game_id", (pl.col("total") / pl.col("n")).fill_null(0.0).alias(side))
        )
        out = out.drop(side).join(known, on="game_id")
    return out.select("game_id", gap=pl.col("home") - pl.col("away"))


def surprises(read: ys.Read) -> pl.DataFrame:
    """Each game of read's frame with surprise: either team's starter (from its boxscore) was not
    its likeliest candidate among the start probabilities known before the opener. Null without
    a boxscore."""
    moments = read.frame.select("game_id", "prediction_utc")
    starts = read.b2_tables.goalie_starts.join(moments, on="game_id").filter(
        pl.col("observed_utc") < pl.col("prediction_utc")
    )
    likeliest = starts.sort("p_start", descending=True).group_by("game_id", "team").first()
    starters = team_goalie_games(read.b2_tables.actual_lineups).select("game_id", "team", "starter")
    teams = likeliest.select("game_id", "team", "goalie_id").join(
        starters, on=["game_id", "team"], how="left"
    )
    return teams.group_by("game_id").agg(
        surprise=pl.when(pl.col("starter").is_null().any())
        .then(None)
        .otherwise((pl.col("starter") != pl.col("goalie_id")).any())
    )


def subsets(read: ys.Read) -> pl.DataFrame:
    """Each game of the explore half with a flag per subset (SUBSETS) and its squared move."""
    frame = read.frame.filter(pl.col("season").is_in(list(ys.EXPLORE)))
    travel = b2.schedule_inputs(read.b2_tables.schedule_terms).select("game_id", "travel_diff")
    flags = (
        frame.select("game_id", "season", "game_date", "rested", moved=pl.col("y") ** 2)
        .join(game_numbers(read.games), on="game_id")
        .join(travel, on="game_id")
        .join(luck_gaps(read.frame, read.games), on="game_id")
        .join(surprises(read), on="game_id", how="left")
    )
    far = flags["travel_diff"].abs().quantile(TOP)
    lucky = flags["gap"].abs().quantile(TOP)
    early, back, travel_, ahead, goalie = SUBSETS
    return flags.select(
        "game_id",
        "season",
        "game_date",
        "moved",
        ((pl.col("home_n") <= EARLY) & (pl.col("away_n") <= EARLY)).alias(early),
        (~pl.col("rested")).alias(back),
        (pl.col("travel_diff").abs() >= far).alias(travel_),
        (pl.col("gap").abs() >= lucky).alias(ahead),
        pl.col("surprise").fill_null(False).alias(goalie),
    )


def _estimate(frame: pl.DataFrame, value: str) -> dict[str, Any]:
    return metrics.bootstrap(frame, value).to_dict()


def parts(per_game: pl.DataFrame) -> dict[str, dict[str, Any]]:
    """Each part's addition to all of them: all less all without the part, game by game."""
    return {part: ys.paired(per_game, "all", f"all less {part}") for part in ys.CANDIDATES["all"]}


def where(per_game: pl.DataFrame, flags: pl.DataFrame) -> dict[str, dict[str, Any]]:
    """Per subset and its complement: the games, the squared move, each set's move skill, and B3's
    split less B2's inputs on the games both scored."""
    found: dict[str, dict[str, Any]] = {}
    for subset in SUBSETS:
        for inside, label in ((True, subset), (False, f"not {subset}")):
            games = flags.filter(pl.col(subset) == inside)
            if games.is_empty():
                found[label] = {"games": 0}
                continue
            row: dict[str, Any] = {
                "games": games.height,
                "moved": _estimate(games, "moved"),
            }
            for name in SETS:
                scored = per_game.filter(pl.col("candidate") == name)
                row[name] = _estimate(scored.join(games.select("game_id"), on="game_id"), "move")
            inside_games = per_game.join(games.select("game_id"), on="game_id")
            row["split less B2"] = ys.paired(inside_games, "B3 split", "B2 inputs")
            found[label] = row
    return found


def _cell(estimate: Mapping[str, Any]) -> str:
    return (
        f"{estimate['mean'] * 1000:+.3f} [{estimate['low'] * 1000:+.3f}, "
        f"{estimate['high'] * 1000:+.3f}]"
    )


def markdown(added: Mapping[str, Any], found: Mapping[str, Any], version: str) -> str:
    """The diagnostics report: the parts, then the subsets, in thousandths with intervals."""
    lines = [
        f"# Yardstick diagnostics: explore, {version}",
        "",
        "Step 2 of #361 (ADR 0053), on the explore half (2012-13 to 2014-15) only. Figures are in",
        "thousandths of squared log-odds with 95% weekly block bootstrap intervals. Negative move",
        "skill means a forecast of SBR's move from the opener to the close beats the opener's own",
        "drift. With this many figures, some intervals exclude zero by chance: a finding is a",
        "claim to score once on the confirm half, not a result.",
        "",
        "## What each part adds",
        "",
        "All the features together less all of them without the part, game by game. Negative",
        "means the part adds beyond the others.",
        "",
        "| Part | All less all without it |",
        "| --- | --- |",
    ]
    lines += [f"| {part} | {_cell(row)} |" for part, row in added.items()]
    lines += [
        "",
        "## Where the market learns late",
        "",
        "Each subset (definitions in `scripts/yardstick_diagnostics.py`) and its complement: the",
        "squared move (where the market moves most), the move skill of B2's inputs, B3's split and",
        "all of them, and B3's split less B2's inputs on the games both scored.",
        "",
        "| Games | Count | Squared move | B2 inputs | B3 split | All | Split less B2 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label, row in found.items():
        if not row["games"]:
            lines.append(f"| {label} | 0 | | | | | |")
            continue
        lines.append(
            f"| {label} | {row['games']:,} | {_cell(row['moved'])} | {_cell(row['B2 inputs'])} "
            f"| {_cell(row['B3 split'])} | {_cell(row['all'])} | {_cell(row['split less B2'])} |"
        )
    return "\n".join(lines) + "\n"


def main(seasons: Sequence[int] = ys.EXPLORE) -> None:
    parser = argparse.ArgumentParser(description="The yardstick's diagnostics (#361 step 2).")
    parser.add_argument("--out", type=Path, default=Path("reports/yardstick"))
    args = parser.parse_args()
    read = ys.read(Lake(), max(seasons))
    per_game, _ = ys.run(read.frame, "explore", candidates())
    flags = subsets(read)
    version = reports.version("yardstick-diagnostics", datetime.now(UTC))
    report = markdown(parts(per_game), where(per_game, flags), version)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"diagnostics-{version}.md"
    path.write_text(report)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
