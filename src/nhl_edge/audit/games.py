"""Missing and duplicate games: the games table against each season's length (EXPECTED_GAMES) and
against every regular-season game the cached NHL schedule listings name.

The listings are the schedule responses under nhl/schedule/ in the raw cache: the nightly ingest's
weeks and the odds job's daily checks. They name every game, final or not, so a game that was
listed but never became final, or a final game no listing names, shows up here. A game counts at
its newest listing, the one that knows about any postponement.

A season is compared with its expected length only once it is over by the audit date: on its last
listed regular-season date, or July 1 of its second year when no listing names it (no regular
season runs into July). So a season missing from both the lake and the raw cache is still found.
"""

import json
from datetime import date

import polars as pl

from nhl_edge.ingest.games import EXPECTED_GAMES
from nhl_edge.ingest.nhl_api import REGULAR_SEASON, parse_utc
from nhl_edge.ingest.odds_lake import SCHEDULE_PREFIX, dated_raw_keys, is_complete
from nhl_edge.lake.raw import RawStore

LISTED_SCHEMA = {
    "game_id": pl.Int64,
    "season": pl.Int32,
    "game_type": pl.Int8,
    "game_date": pl.Date,
    "start_utc": pl.Datetime("us", "UTC"),
    "home": pl.String,
    "away": pl.String,
    "game_state": pl.String,
    "listed_key": pl.String,
}
# How many game ids a problem line names.
EXAMPLES = 5
# The season report's counts after number_gaps; the last three follow disagreements' kinds.
COUNTS = ["team_twice_a_day", "repeated_matchups", "not_listed", "date_differs", "missing"]


def listed_games(store: RawStore) -> pl.DataFrame:
    """Every game in the cached schedule listings, of every game type, at its newest listing."""
    rows = []
    for keys in dated_raw_keys(SCHEDULE_PREFIX, store).values():
        for raw_key in keys:
            if not is_complete(store, raw_key):
                continue
            for day in json.loads(store.get(raw_key))["gameWeek"]:
                rows.extend(
                    {
                        "game_id": game["id"],
                        "season": game["season"],
                        "game_type": game["gameType"],
                        "game_date": date.fromisoformat(day["date"]),
                        "start_utc": parse_utc(game["startTimeUTC"]),
                        "home": game["homeTeam"]["abbrev"],
                        "away": game["awayTeam"]["abbrev"],
                        "game_state": game["gameState"],
                        "listed_key": raw_key,
                    }
                    for game in day["games"]
                )
    listed = pl.DataFrame(rows, schema=LISTED_SCHEMA)
    # Keys end in the fetch stamp, so the newest listing of a game has the largest stamp.
    stamp = pl.col("listed_key").str.split("/").list.last()
    return listed.sort(stamp, "listed_key").unique("game_id", keep="last").sort("game_id")


def disagreements(
    games: pl.DataFrame, listed: pl.DataFrame, as_of: date
) -> dict[str, pl.DataFrame]:
    """The games on which the games table and the listings disagree, by kind: season, game_id and
    the listed state. A listed game counts as missing only if it was dated on or before as_of, the
    last day the audit covers, so games still to be played are not."""
    listed = listed.filter(pl.col("game_type") == REGULAR_SEASON)
    listing = listed.select("game_id", listed_date="game_date", listed_state="game_state")
    unlisted = games.join(listing, on="game_id", how="anti").with_columns(
        listed_state=pl.lit(None, pl.String)
    )
    moved = games.join(listing, on="game_id").filter(pl.col("listed_date") != pl.col("game_date"))
    missing = (
        listed.filter(pl.col("game_date") <= as_of)
        .join(games.select("game_id"), on="game_id", how="anti")
        .rename({"game_state": "listed_state"})
    )
    columns = ["season", "game_id", "listed_state"]
    return {
        "final but in no listing": unlisted.select(columns),
        "dated differently from their newest listing": moved.select(columns),
        "listed by the audit date but not in games": missing.select(columns),
    }


def _count(grouped: pl.DataFrame, name: str) -> pl.DataFrame:
    """How many groups per season hold more than one game."""
    return grouped.filter(pl.col("len") > 1).group_by("season").agg(pl.len().alias(name))


def seasons_over(listed: pl.DataFrame, as_of: date) -> list[int]:
    """The seasons of EXPECTED_GAMES whose regular season is over by as_of."""
    last_listed = dict(
        listed.filter(pl.col("game_type") == REGULAR_SEASON)
        .group_by("season")
        .agg(pl.col("game_date").max())
        .iter_rows()
    )
    return [
        season
        for season in EXPECTED_GAMES
        if as_of >= last_listed.get(season, date(season % 10_000, 7, 1))
    ]


def season_report(games: pl.DataFrame, listed: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """One row per season in games or over by as_of: its games against its expected length once
    it is over, gaps in its game numbers, duplicates, and the disagreements with the listings."""
    over = seasons_over(listed, as_of)
    seasons = sorted(set(games["season"].to_list()) | set(over))
    expected = {season: EXPECTED_GAMES[season] for season in over}
    teams = pl.concat(
        [
            games.select("season", "game_date", team="home"),
            games.select("season", "game_date", team="away"),
        ]
    )
    report = pl.DataFrame({"season": seasons}, schema={"season": pl.Int32}).with_columns(
        pl.col("season")
        .replace_strict(expected, default=None, return_dtype=pl.Int64)
        .alias("expected")
    )
    parts = [
        games.group_by("season").agg(
            pl.len().alias("games"),
            ((pl.col("game_id") % 10_000).max() - pl.len()).alias("number_gaps"),
        ),
        teams.group_by("season", "team")
        .len()
        .group_by("season")
        .agg(
            pl.len().alias("teams"),
            pl.col("len").min().alias("team_games_min"),
            pl.col("len").max().alias("team_games_max"),
        ),
        _count(teams.group_by("season", "game_date", "team").len(), "team_twice_a_day"),
        _count(games.group_by("season", "game_date", "home", "away").len(), "repeated_matchups"),
    ]
    for name, frame in zip(COUNTS[2:], disagreements(games, listed, as_of).values(), strict=True):
        parts.append(frame.group_by("season").agg(pl.len().alias(name)))
    for part in parts:
        report = report.join(part, on="season", how="left")
    return (
        report.with_columns(pl.col("games", "number_gaps", "teams", *COUNTS).fill_null(0))
        .select(
            "season",
            "games",
            "expected",
            "teams",
            "team_games_min",
            "team_games_max",
            "number_gaps",
            *COUNTS,
        )
        .sort("season")
    )


def problems(
    report: pl.DataFrame, games: pl.DataFrame, listed: pl.DataFrame, as_of: date
) -> list[str]:
    """One line per season and kind of problem, naming example games."""
    lines = []
    for row in report.iter_rows(named=True):
        season = row["season"]
        if row["expected"] is not None and row["games"] != row["expected"]:
            lines.append(f"{season}: {row['games']} games, expected {row['expected']}")
        if row["expected"] is not None and row["number_gaps"]:
            # Under way, a postponed game leaves a gap until it is played.
            lines.append(f"{season}: {row['number_gaps']} game numbers missing below the highest")
        if row["team_twice_a_day"]:
            lines.append(f"{season}: {row['team_twice_a_day']} times a team plays twice a day")
        if row["repeated_matchups"]:
            lines.append(f"{season}: {row['repeated_matchups']} matchups repeated on one date")
    for label, frame in disagreements(games, listed, as_of).items():
        by_season = (
            frame.sort("game_id")
            .group_by("season", maintain_order=True)
            .agg(pl.col("game_id"), pl.col("listed_state"))
            .sort("season")
        )
        for season, ids, states in by_season.iter_rows():
            examples = ", ".join(
                f"{game_id} ({state})" if state else str(game_id)
                for game_id, state in list(zip(ids, states, strict=True))[:EXAMPLES]
            )
            lines.append(f"{season}: {len(ids)} games {label}, e.g. {examples}")
    return lines


def markdown_report(report: pl.DataFrame) -> str:
    header = (
        "| Season | Games | Expected | Teams | Games per team | Number gaps | Team twice a day "
        "| Repeated matchups | Not listed | Date differs | Listed, not in games |\n"
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    lines = []
    for r in report.iter_rows(named=True):
        expected = f"{r['expected']:,}" if r["expected"] is not None else "under way"
        low, high = r["team_games_min"], r["team_games_max"]
        per_team = "" if low is None else str(low) if low == high else f"{low} to {high}"
        lines.append(
            f"| {r['season']} | {r['games']:,} | {expected} | {r['teams']} | {per_team} "
            f"| {r['number_gaps']} | {r['team_twice_a_day']} | {r['repeated_matchups']} "
            f"| {r['not_listed']} | {r['date_differs']} | {r['missing']} |"
        )
    return "\n".join([header, *lines])
