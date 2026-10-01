"""The audit report's penalties and faceoffs section (#96): per season, how many games have
faceoffs, how often a penalty names the player who took it and the one who drew it, and whether
the minutes of each team's penalties with a player add up to its players' penalty minutes in the
boxscore. For the seasons open now, it also gives penalties, minors and faceoffs per game; a
held-out season keeps its checks but not its rates, which a feature could be shaped by.

A team-game whose minutes differ is a problem. Over the 31,288 team-games of 2010-11 to 2021-22
the two disagree in 20: 5 by 10 minutes (misconducts, two of them logged in a shootout and so
left out of penalties), 11 with a minor more in the boxscore, 3 with one fewer, and 1 by 5.
"""

from collections.abc import Collection
from concurrent.futures import ThreadPoolExecutor

import polars as pl

from nhl_edge.ingest.plays import boxscore_pim
from nhl_edge.lake.raw import RawStore

EXAMPLES = 3
WORKERS = 16
MINORS = ("MIN", "BEN")
PIM_SCHEMA = {"game_id": pl.Int64, "team": pl.String, "box_pim": pl.Int64}


def boxscore_pims(store: RawStore, lineups: pl.DataFrame) -> pl.DataFrame:
    """Each team-game's penalty minutes from the boxscore its actual_lineups rows were parsed
    from (game_id, team, box_pim)."""
    keys = lineups.group_by("game_id").agg(pl.col("raw_key").first()).sort("game_id")

    def read(raw_key: str) -> dict[str, int]:
        return boxscore_pim(store.get(raw_key))

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        pims = list(pool.map(read, keys["raw_key"].to_list()))
    rows = [
        {"game_id": game_id, "team": team, "box_pim": pim}
        for game_id, by_team in zip(keys["game_id"].to_list(), pims, strict=True)
        for team, pim in by_team.items()
    ]
    return pl.DataFrame(rows, schema=PIM_SCHEMA)


def pim_check(penalties: pl.DataFrame, box: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """Each team-game of box with its season and the minutes of its penalties that name a player
    (feed_pim), 0 when it has none."""
    feed = (
        penalties.filter(pl.col("committed_by").is_not_null())
        .group_by("game_id", "team")
        .agg(feed_pim=pl.col("duration_min").cast(pl.Int64).sum())
    )
    return (
        box.join(games.select("game_id", "season"), on="game_id")
        .join(feed, on=["game_id", "team"], how="left")
        .with_columns(pl.col("feed_pim").fill_null(0))
    )


def season_report(
    penalties: pl.DataFrame,
    faceoffs: pl.DataFrame,
    checked: pl.DataFrame,
    games: pl.DataFrame,
    open_seasons: Collection[int],
) -> pl.DataFrame:
    """Per season: games, those with faceoffs, the share of penalties naming the player who took
    it and the one who drew it, team-games whose penalty minutes agree with the boxscore, and,
    for open_seasons only, penalties, minors and faceoffs per game."""
    per_penalty = penalties.group_by("season").agg(
        penalties=pl.len(),
        minors=pl.col("type_code").is_in(MINORS).sum(),
        committer=pl.col("committed_by").is_not_null().mean(),
        drawer=pl.col("drawn_by").is_not_null().mean(),
    )
    per_faceoff = faceoffs.group_by("season").agg(
        faceoffs=pl.len(), with_faceoffs=pl.col("game_id").n_unique()
    )
    agree = checked.group_by("season").agg(
        team_games=pl.len(),
        pim_agree=(pl.col("feed_pim") == pl.col("box_pim")).sum(),
    )
    shown = pl.col("season").is_in(list(open_seasons))
    rates = ("penalties", "minors", "faceoffs")
    return (
        games.group_by("season")
        .agg(games=pl.len())
        .join(per_faceoff, on="season", how="left")
        .join(per_penalty, on="season", how="left")
        .join(agree, on="season", how="left")
        .with_columns(pl.col("with_faceoffs", "team_games", "pim_agree").fill_null(0))
        .with_columns(
            pl.when(shown)
            .then((pl.col(c).fill_null(0) / pl.col("games")).round(1))
            .otherwise(None)
            .alias(c)
            for c in rates
        )
        .sort("season")
    )


def markdown_report(report: pl.DataFrame) -> str:
    lines = [
        "| Season | Games | With faceoffs | Penalties naming who took it | Who drew it "
        "| Team-games whose PIM agree | Penalties per game | Minors | Faceoffs |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report.iter_rows(named=True):
        shares = " | ".join(
            "" if row[c] is None else f"{row[c]:.1%}" for c in ("committer", "drawer")
        )
        if row["penalties"] is None:
            rates = "| held out | | "
        else:
            rates = f"| {row['penalties']:.1f} | {row['minors']:.1f} | {row['faceoffs']:.1f} "
        lines.append(
            f"| {row['season']} | {row['games']:,} | {row['with_faceoffs']:,} | {shares} "
            f"| {row['pim_agree']:,} of {row['team_games']:,} " + rates + "|"
        )
    return "\n".join(lines)


def problems(faceoffs: pl.DataFrame, checked: pl.DataFrame, games: pl.DataFrame) -> list[str]:
    found = []
    off = checked.filter(pl.col("feed_pim") != pl.col("box_pim")).sort("game_id", "team")
    for (season,), rows in off.group_by("season", maintain_order=True):
        examples = ", ".join(
            f"{game_id} {team} ({feed} min in play-by-play, {box} in the boxscore)"
            for game_id, team, feed, box in rows.select("game_id", "team", "feed_pim", "box_pim")
            .head(EXAMPLES)
            .rows()
        )
        tens = rows.filter((pl.col("box_pim") - pl.col("feed_pim")) % 10 == 0).height
        found.append(
            f"{season}: {rows.height} team-games whose penalty minutes differ from the boxscore's, "
            f"{tens} of them by a multiple of 10, e.g. {examples}"
        )
    missing = games.join(faceoffs.select("game_id").unique(), on="game_id", how="anti")
    for (season,), rows in missing.sort("game_id").group_by("season", maintain_order=True):
        examples = ", ".join(str(g) for g in rows["game_id"].head(EXAMPLES).to_list())
        found.append(f"{season}: {rows.height} games without faceoffs, e.g. {examples}")
    return found
