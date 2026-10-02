"""The audit report's player league seasons section (#98): per season, the players with lines and
the lines in the leagues with the most of them. It shows counts only, for every season. The
landing pages hold the goals and assists of held-out seasons, which must stay unseen, and one rule
for all seasons keeps the section simple.

A player in players without a cached landing page is a problem: his lines are missing until his
page is fetched.
"""

import polars as pl

from nhl_edge.ingest.player_seasons import LANDING_PREFIX
from nhl_edge.lake.raw import RawStore

TOP_LEAGUES = 8
EXAMPLES = 10


def top_leagues(frame: pl.DataFrame, n: int = TOP_LEAGUES) -> list[str]:
    """The n leagues with the most lines over all seasons, most first, ties by name."""
    counts = frame.group_by("league").len().sort(["len", "league"], descending=[True, False])
    return counts["league"].head(n).to_list()


def season_report(frame: pl.DataFrame, leagues: list[str]) -> pl.DataFrame:
    """Per season: the players with lines, the lines (rows of the table: one per player, league
    abbreviation and game type), the lines in each of leagues, and those in every other league."""
    return (
        frame.group_by("season")
        .agg(
            pl.col("player_id").n_unique().alias("players"),
            pl.len().alias("lines"),
            *((pl.col("league") == league).sum().alias(league) for league in leagues),
            (~pl.col("league").is_in(leagues)).sum().alias("other"),
        )
        .sort("season")
    )


def shared_leagues(frame: pl.DataFrame) -> int:
    """Player-season-game types with lines of one league under two abbreviations. A reader that
    adds them up by league must check them: some are one season listed under both names."""
    keys = ("player_id", "season", "game_type", "league")
    return frame.group_by(keys).len().filter(pl.col("len") > 1).height


def markdown_report(report: pl.DataFrame, leagues: list[str]) -> str:
    header = ["Season", "Players", "Lines", *leagues, "Other leagues"]
    lines = ["| " + " | ".join(header) + " |", "| --- |" + " ---: |" * (len(header) - 1)]
    for row in report.iter_rows(named=True):
        counts = (row[column] for column in ("players", "lines", *leagues, "other"))
        lines.append(f"| {row['season']} | " + " | ".join(f"{n:,}" for n in counts) + " |")
    return "\n".join(lines)


def problems(players: pl.DataFrame, store: RawStore) -> list[str]:
    """Players in players without a landing page in the raw cache."""
    missing = [
        player_id
        for player_id in sorted(players["player_id"].to_list())
        if store.latest(f"{LANDING_PREFIX}/{player_id}") is None
    ]
    if not missing:
        return []
    examples = ", ".join(map(str, missing[:EXAMPLES]))
    return [
        f"{len(missing)} players in players have no landing page in the raw cache, e.g. "
        f"{examples}: nhl lake restore-raw brings in those R2 has"
    ]
