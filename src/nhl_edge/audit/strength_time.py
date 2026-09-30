"""The audit report's strength time section (#72): per season, whether each game's seconds at every
strength state add up to its length, and the typical minutes a team spends at 5v5, on the power
play and short-handed. A game whose seconds do not add up, or that has none, is a problem."""

from collections.abc import Collection

import polars as pl

from nhl_edge.lake.schemas import STRENGTH_SOURCES

EXAMPLES = 3
POWER_PLAY = ("5v4", "5v3", "4v3", "6v4", "6v3")
SHORT_HANDED = ("4v5", "3v5", "3v4", "4v6", "3v6")


def season_report(
    frame: pl.DataFrame, games: pl.DataFrame, open_seasons: Collection[int]
) -> pl.DataFrame:
    """Per season: games, those whose seconds add up for both teams, and, for open_seasons only,
    per team-game the mean 5v5 minutes with both nets manned, power-play and short-handed
    minutes, and the share of all seconds whose counts came from the chart. A held-out season
    keeps its checks but not its minutes, which a feature could be shaped by."""
    totals = frame.group_by("season", "game_id", "team").agg(
        pl.col("seconds").sum(), pl.col("game_seconds").first()
    )
    whole = totals.group_by("season", "game_id").agg(
        adds_up=(pl.col("seconds") == pl.col("game_seconds")).all()
    )
    both_in = ~pl.col("own_net_empty") & ~pl.col("opp_net_empty")

    def minutes(condition: pl.Expr) -> pl.Expr:
        return pl.col("seconds").filter(condition).sum() / 60

    per_season = frame.group_by("season").agg(
        team_games=pl.struct("game_id", "team").n_unique(),
        even=minutes(both_in & (pl.col("strength") == "5v5")),
        power_play=minutes(pl.col("strength").is_in(POWER_PLAY)),
        short_handed=minutes(pl.col("strength").is_in(SHORT_HANDED)),
        chart_share=pl.col("seconds").filter(pl.col("strength_source") == STRENGTH_SOURCES[1]).sum()
        / pl.col("seconds").sum(),
    )
    counts = (
        games.group_by("season")
        .agg(games=pl.len())
        .join(
            whole.group_by("season").agg(
                with_time=pl.len(), adds_up=pl.col("adds_up").sum().cast(pl.UInt32)
            ),
            on="season",
            how="left",
        )
    )
    shown = pl.col("season").is_in(list(open_seasons))
    return (
        counts.join(per_season, on="season", how="left")
        .with_columns(
            (pl.col(c) / pl.col("team_games")).round(1)
            for c in ("even", "power_play", "short_handed")
        )
        .with_columns(pl.col("with_time", "adds_up").fill_null(0))
        .with_columns(
            pl.when(shown).then(pl.col(c).fill_null(0)).otherwise(None).alias(c)
            for c in ("even", "power_play", "short_handed", "chart_share")
        )
        .sort("season")
    )


def markdown_report(report: pl.DataFrame) -> str:
    lines = [
        "| Season | Games | With strength time | Seconds add up | 5v5 min per team-game "
        "| Power play | Short-handed | From the chart |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report.iter_rows(named=True):
        if row["even"] is None:
            minutes = "| held out | | | "
        else:
            minutes = (
                f"| {row['even']:.1f} | {row['power_play']:.1f} | {row['short_handed']:.1f} "
                f"| {row['chart_share']:.1%} "
            )
        lines.append(
            f"| {row['season']} | {row['games']:,} | {row['with_time']:,} | {row['adds_up']:,} "
            + minutes
            + "|"
        )
    return "\n".join(lines)


def problems(frame: pl.DataFrame, games: pl.DataFrame) -> list[str]:
    found = []
    totals = frame.group_by("season", "game_id", "team").agg(
        pl.col("seconds").sum(), pl.col("game_seconds").first()
    )
    off = totals.filter(pl.col("seconds") != pl.col("game_seconds")).sort("game_id")
    for (season,), rows in off.group_by("season", maintain_order=True):
        examples = ", ".join(
            f"{game_id} {team} ({seconds} of {length} s)"
            for game_id, team, seconds, length in rows.select(
                "game_id", "team", "seconds", "game_seconds"
            )
            .head(EXAMPLES)
            .rows()
        )
        games_off = rows["game_id"].n_unique()
        found.append(f"{season}: {games_off} games whose seconds do not add up, e.g. {examples}")
    missing = games.join(frame.select("game_id").unique(), on="game_id", how="anti")
    for (season,), rows in missing.sort("game_id").group_by("season", maintain_order=True):
        examples = ", ".join(str(g) for g in rows["game_id"].head(EXAMPLES).to_list())
        found.append(f"{season}: {rows.height} games without strength time, e.g. {examples}")
    return found
