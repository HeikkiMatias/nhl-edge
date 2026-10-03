"""Gate 2's subsets (#106, ADR 0023): the games after trades, injuries and lineup changes, fixed
from boxscores and the projection before any B3 result is seen.

A game is in a subset if either team qualifies:
- **trade:** a projected skater (p_available at least IN_LINEUP) who dressed for another team's
  line in any of his last WINDOW games before this one;
- **injury:** a regular missing from the projection (not a candidate, or p_available below
  IN_LINEUP), a regular being among the team's REGULARS["F"] forwards or REGULARS["D"]
  defensemen with the most ice time over its last WINDOW games;
- **lineup change:** CHANGED or more of the skaters who dressed in the team's previous game
  missing from the projection, by the same test.

Each reads only boxscores public before the game's as-of time (team strength's) and the game's
projection, which itself reads only earlier boxscores (ADR 0017). A team's games follow its line
through code changes (team_strength.team_lines), so a relocation is not a trade.
"""

from collections.abc import Mapping

import polars as pl

from nhl_edge.features import team_strength as ts

WINDOW = 10
IN_LINEUP = 0.5
REGULARS = {"F": 9, "D": 4}
CHANGED = 3
SUBSETS = ("trade", "injury", "lineup_change")


def _skater_games(boxscores: pl.DataFrame, lines: Mapping[str, str]) -> pl.DataFrame:
    """Each skater-game of the boxscores with the team's line and ice time, in order of
    publication per team (k) and per player (j)."""
    rows = (
        boxscores.filter(pl.col("role").is_in(["F", "D"]))
        .select(
            "game_id",
            "game_date",
            "team",
            "player_id",
            "role",
            toi=pl.col("toi_s").fill_null(0).cast(pl.Float64),
            observed_utc="observed_utc",
            line=pl.col("team").replace(dict(lines)),
        )
        .sort("observed_utc", "game_date", "game_id")
    )
    team_games = (
        rows.select("line", "game_id", "observed_utc", "game_date")
        .unique(["line", "game_id"])
        .sort("observed_utc", "game_date", "game_id")
        .with_columns(k=pl.int_range(pl.len()).over("line"))
    )
    player_games = (
        rows.select("player_id", "game_id", "observed_utc", "game_date")
        .unique(["player_id", "game_id"])
        .sort("observed_utc", "game_date", "game_id")
        .with_columns(j=pl.int_range(pl.len()).over("player_id"))
    )
    return rows.join(team_games.select("line", "game_id", "k"), on=["line", "game_id"]).join(
        player_games.select("player_id", "game_id", "j"), on=["player_id", "game_id"]
    )


def _seen(history: pl.DataFrame, targets: pl.DataFrame, by: str, index: str) -> pl.DataFrame:
    """targets (by, as_of_utc) with n, how many of the by's games were public before
    as_of_utc."""
    published = history.select(by, "observed_utc", index).unique().sort("observed_utc")
    return (
        targets.sort("as_of_utc")
        .join_asof(
            published.rename({"observed_utc": "public_utc"}),
            left_on="as_of_utc",
            right_on="public_utc",
            by=by,
            strategy="backward",
            allow_exact_matches=False,
            check_sortedness=False,
        )
        .with_columns(n=(pl.col(index) + 1).fill_null(0))
        .drop(index, "public_utc")
    )


def flags(
    games: pl.DataFrame,
    boxscores: pl.DataFrame,
    projections: pl.DataFrame,
    seasons: list[int],
    lines: Mapping[str, str] | None = None,
) -> pl.DataFrame:
    """One row per game of the seasons: whether it is in each subset (SUBSETS) and in any.
    boxscores are actual_lineups (with toi_s); projections are lineups (the skaters'
    p_available)."""
    lines = ts.team_lines() if lines is None else lines
    history = _skater_games(boxscores, lines)
    targets = pl.concat(
        [
            games.filter(pl.col("season").is_in(seasons)).select(
                "game_id",
                team=pl.col(side),
                as_of_utc=ts.as_of(pl.col("game_date"), pl.col("start_utc")),
            )
            for side in ("home", "away")
        ]
    ).with_columns(line=pl.col("team").replace(dict(lines)))
    projected = projections.filter(pl.col("role").is_in(["F", "D"])).select(
        "game_id", "team", "player_id", "p_available"
    )
    playing = projected.filter(pl.col("p_available") >= IN_LINEUP).select(
        "game_id", "team", "player_id"
    )

    # The team's last WINDOW games public before the as-of time, and its previous one.
    seen = _seen(history, targets, "line", "k")
    recent = (
        seen.join(history.select("line", "k", "player_id", "role", "toi"), on="line")
        .filter(pl.col("k") >= pl.col("n") - WINDOW, pl.col("k") < pl.col("n"))
        .select(pl.col("game_id").alias("target"), "team", "player_id", "role", "toi", "k", "n")
    )
    regulars = (
        recent.group_by("target", "team", "player_id", "role")
        .agg(pl.col("toi").sum())
        .with_columns(
            rank=pl.struct(pl.col("toi").neg(), "player_id")
            .rank("ordinal")
            .over("target", "team", "role")
        )
        .filter(
            ((pl.col("role") == "F") & (pl.col("rank") <= REGULARS["F"]))
            | ((pl.col("role") == "D") & (pl.col("rank") <= REGULARS["D"]))
        )
        .select(pl.col("target").alias("game_id"), "team", "player_id")
    )
    injury = (
        regulars.join(playing, on=["game_id", "team", "player_id"], how="anti")
        .select("game_id", "team")
        .unique()
        .with_columns(injury=pl.lit(True))
    )
    previous = (
        recent.filter(pl.col("k") == pl.col("n") - 1)
        .select(pl.col("target").alias("game_id"), "team", "player_id")
        .unique()
    )
    changed = (
        previous.join(playing, on=["game_id", "team", "player_id"], how="anti")
        .group_by("game_id", "team")
        .len()
        .filter(pl.col("len") >= CHANGED)
        .select("game_id", "team", lineup_change=pl.lit(True))
    )

    # Each projected skater's last WINDOW games public before the as-of time.
    skaters = playing.join(
        targets.select("game_id", "team", "as_of_utc", "line"), on=["game_id", "team"]
    )
    mine = _seen(history, skaters, "player_id", "j")
    elsewhere = (
        mine.join(history.select("player_id", "j", other="line"), on="player_id")
        .filter(
            pl.col("j") >= pl.col("n") - WINDOW,
            pl.col("j") < pl.col("n"),
            pl.col("other") != pl.col("line"),
        )
        .select("game_id", "team")
        .unique()
        .with_columns(trade=pl.lit(True))
    )

    teams = (
        targets.select("game_id", "team")
        .join(elsewhere, on=["game_id", "team"], how="left")
        .join(injury, on=["game_id", "team"], how="left")
        .join(changed, on=["game_id", "team"], how="left")
        .with_columns(pl.col(*SUBSETS).fill_null(False))
    )
    return (
        teams.group_by("game_id")
        .agg(pl.col(*SUBSETS).any())
        .with_columns(any=pl.any_horizontal(*SUBSETS))
        .sort("game_id")
    )
