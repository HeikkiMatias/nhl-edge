"""The audit report's player league seasons section (#98): per season, the players with lines and
the lines in the leagues with the most of them. It shows counts only: the landing pages hold the
goals and assists of held-out seasons, which must stay unseen, and one rule for all seasons keeps
the section simple. As the stints section, it leaves out the one-time test season and the live
seasons, which no design choice may see even as counts, and the players who debuted in them, whose
earlier lines would show it. The leagues with the most lines are picked without them too.

A player in players without a cached landing page is a problem: his lines are missing until his
page is fetched. So is a row whose first_boxscore_utc is not the player's first boxscore in
actual_lineups: the table was built before the boxscores changed and needs a rebuild.
"""

import polars as pl

from nhl_edge.backtest.seasons import FIRST_LIVE_SEASON, SEASON_ROLES, SeasonRole
from nhl_edge.ingest.player_seasons import LANDING_PREFIX
from nhl_edge.lake.raw import RawStore

TOP_LEAGUES = 8
EXAMPLES = 10
HIDDEN = [s for s, role in SEASON_ROLES.items() if role is SeasonRole.ONE_TIME_TEST]


def shown(season: pl.Expr) -> pl.Expr:
    """The seasons the section shows: all before the live seasons but the one-time test season."""
    return (season < FIRST_LIVE_SEASON) & ~season.is_in(HIDDEN)


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


def drop_hidden_debuts(frame: pl.DataFrame, lineups: pl.DataFrame) -> pl.DataFrame:
    """frame without the players whose first boxscore in lineups (actual_lineups) is in a season
    the section hides: their earlier lines would show who debuted in the one-time test or a live
    season."""
    debuts = (
        lineups.sort("observed_utc")
        .group_by("player_id")
        .agg(debut_season=pl.col("season").first())
        .filter(shown(pl.col("debut_season")))
    )
    return frame.join(debuts, on="player_id", how="semi")


def first_game_problems(frame: pl.DataFrame, lineups: pl.DataFrame) -> list[str]:
    """Players whose first_boxscore_utc differs from their first boxscore in lineups
    (actual_lineups), or who have none there."""
    first = lineups.group_by("player_id").agg(first=pl.col("observed_utc").min())
    wrong = (
        frame.group_by("player_id")
        .agg(pl.col("first_boxscore_utc").first())
        .join(first, on="player_id", how="left")
        .filter(pl.col("first").is_null() | (pl.col("first") != pl.col("first_boxscore_utc")))
        .sort("player_id")
    )
    if wrong.is_empty():
        return []
    examples = ", ".join(map(str, wrong["player_id"].head(EXAMPLES).to_list()))
    return [
        f"{wrong.height} players' first_boxscore_utc is not their first boxscore in "
        f"actual_lineups, e.g. {examples}: rerun nhl player-seasons"
    ]


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
