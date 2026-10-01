"""The audit report's stints section (#97, ADR 0015): per season, the games whose complete shift
chart gives stints, the stints RAPM leaves out and why, and how many faceoffs open a stint and so
give it a zone. For the seasons open now, it also gives the share of the season's playing time
and xG in the stints RAPM keeps; a held-out season keeps its counts but not its shares.

A game with a complete chart and no stints is a problem.
"""

from collections.abc import Collection

import polars as pl

from nhl_edge.lake.schemas import STINT_DROPS

EXAMPLES = 3


def season_report(
    stints: pl.DataFrame,
    coverage: pl.DataFrame,
    strength_time: pl.DataFrame,
    shot_xg: pl.DataFrame,
    faceoffs: pl.DataFrame,
    games: pl.DataFrame,
    open_seasons: Collection[int],
) -> pl.DataFrame:
    """Per season: games, those with a complete chart and those with stints, stints and those
    left out per reason with their seconds, the share of faceoffs in games with stints that fall
    on a stint's start, and, for open_seasons only, the shares of all games' seconds (from
    strength_time) and of all xG (shot_xg) in the stints RAPM keeps."""
    reason = pl.col("drop_reason")
    kept = reason.is_null()
    per_stint = stints.group_by("season").agg(
        with_stints=pl.col("game_id").n_unique(),
        stints=pl.len(),
        **{f"dropped_{r}": (reason == r).sum() for r in STINT_DROPS},
        dropped_seconds=pl.col("seconds").filter(~kept).sum(),
        kept_seconds=pl.col("seconds").filter(kept).sum(),
        kept_xg=(pl.col("home_xg") + pl.col("away_xg")).filter(kept).sum(),
    )
    starts = stints.select("game_id", "period", seconds="start_s").unique()
    opened = (
        faceoffs.join(stints.select("game_id").unique(), on="game_id", how="semi")
        .join(
            starts.with_columns(opens=pl.lit(True)), on=["game_id", "period", "seconds"], how="left"
        )
        .group_by("season")
        .agg(faceoff_starts=pl.col("opens").fill_null(False).mean())
    )
    lengths = (
        strength_time.group_by("season", "game_id")
        .agg(pl.col("game_seconds").first())
        .group_by("season")
        .agg(game_seconds=pl.col("game_seconds").sum())
    )
    xg_total = shot_xg.group_by("season").agg(total_xg=pl.col("xg").sum())
    complete = coverage.group_by("season").agg(complete=pl.col("complete").sum())
    counts = [
        "complete",
        "with_stints",
        "stints",
        *(f"dropped_{r}" for r in STINT_DROPS),
        "dropped_seconds",
    ]
    shown = pl.col("season").is_in(list(open_seasons))
    return (
        games.group_by("season")
        .agg(games=pl.len())
        .join(complete, on="season", how="left")
        .join(per_stint, on="season", how="left")
        .join(opened, on="season", how="left")
        .join(lengths, on="season", how="left")
        .join(xg_total, on="season", how="left")
        .with_columns(pl.col(c).fill_null(0) for c in counts)
        .with_columns(
            time_kept=pl.when(shown).then(pl.col("kept_seconds") / pl.col("game_seconds")),
            xg_kept=pl.when(shown).then(pl.col("kept_xg") / pl.col("total_xg")),
        )
        .drop("kept_seconds", "kept_xg", "game_seconds", "total_xg")
        .sort("season")
    )


def _share(value: float | None) -> str:
    return "" if value is None else f"{value:.1%}"


def markdown_report(report: pl.DataFrame) -> str:
    lines = [
        "| Season | Games | Complete charts | With stints | Stints | Left out: skaters "
        "| Goalies | Seconds left out | Faceoffs opening a stint | Time kept | xG kept |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report.iter_rows(named=True):
        if row["time_kept"] is None and row["with_stints"]:
            shares = "| held out | "
        else:
            shares = f"| {_share(row['time_kept'])} | {_share(row['xg_kept'])} "
        lines.append(
            f"| {row['season']} | {row['games']:,} | {row['complete']:,} "
            f"| {row['with_stints']:,} | {row['stints']:,} | {row['dropped_skaters']:,} "
            f"| {row['dropped_goalies']:,} | {row['dropped_seconds']:,} "
            f"| {_share(row['faceoff_starts'])} " + shares + "|"
        )
    return "\n".join(lines)


def problems(stints: pl.DataFrame, coverage: pl.DataFrame) -> list[str]:
    """Games whose chart is complete but have no stints: nhl stints has not run on them."""
    found = []
    complete = coverage.filter(pl.col("complete"))
    missing = complete.join(stints.select("game_id").unique(), on="game_id", how="anti")
    for (season,), rows in missing.sort("game_id").group_by("season", maintain_order=True):
        examples = ", ".join(str(g) for g in rows["game_id"].head(EXAMPLES).to_list())
        found.append(
            f"{season}: {rows.height} games with a complete chart and no stints, e.g. {examples}"
        )
    return found
