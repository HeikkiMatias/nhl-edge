"""Post-game corrections in the per-game feeds (#30, ADR 0004).

The per-game tables keep the play-by-play and boxscore the nightly ingest first fetched, and the
shift chart its lookback settled on, as live does. The 2010-26 backfill read copies fetched years
later, corrections included, so the backtest can see a corrected value that live would not have
had yet. To measure how often that happens, each final game's three feeds are fetched again
RECHECK_AFTER after the tables' copy and stored apart, under <kind>-recheck (NhlApi.recheck), where
no table reads them. Both copies go through the ingest's own parsers (nhl_ingest.parse_feeds), and
diff_game compares the parsed tables row by row.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

from nhl_edge.ingest.nhl_api import (
    FEED_KINDS,
    RECHECK_SUFFIX,
    SOURCE,
    NhlApi,
    NotFoundError,
    parse_utc,
)
from nhl_edge.ingest.nhl_ingest import parse_feeds
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.tables import FEED_TABLES

# A recheck counts once fetched this long after the play-by-play the tables read.
RECHECK_AFTER = timedelta(days=7)
# The rows of each per-game table, within a game.
KEYS: dict[str, list[str]] = {
    "shots": ["event_id"],
    "shifts": ["player_id", "period", "shift_number"],
    "actual_lineups": ["player_id"],
    "shift_coverage": ["game_id"],
}
# Columns that record which copy a row came from and when it counts as public, not what it says,
# and the game's own keys, which both copies share.
NOT_COMPARED = frozenset({"raw_key", "observed_utc", "game_id", "season", "game_date"})
ADDED, REMOVED, CHANGED = "row added", "row removed", "any value"
CHANGE_SCHEMA = {"table": pl.String, "field": pl.String, "rows": pl.Int64}


def due_dates(now: datetime, days: int) -> list[date]:
    """The last `days` game dates whose tables' copies can be RECHECK_AFTER old by now."""
    last = now.date() - RECHECK_AFTER
    return [last - timedelta(days=i) for i in range(days - 1, -1, -1)]


def _entity(game: dict[str, Any]) -> str:
    return f"{game['season']}/{game['game_id']}"


@dataclass
class RecheckSummary:
    dates: list[date]
    games: int = 0
    fetched: int = 0
    reused: int = 0
    not_due: list[int] = field(default_factory=list)
    never_ingested: list[int] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)


def recheck(api: NhlApi, games: pl.DataFrame, now: datetime, days: int) -> RecheckSummary:
    """Fetch the three feeds again for each final game of the last `days` due dates, once the
    play-by-play the tables read is RECHECK_AFTER old. A rerun reuses a recheck it already has."""
    summary = RecheckSummary(due_dates(now, days))
    due = games.filter(pl.col("game_date").is_in(summary.dates)).sort("game_date", "game_id")
    for game in due.iter_rows(named=True):
        first = api.store.latest(f"{SOURCE}/play-by-play/{_entity(game)}")
        if first is None:
            summary.never_ingested.append(game["game_id"])
            continue
        after = parse_utc(api.store.meta(first)["fetched_utc"]) + RECHECK_AFTER
        if after > now:
            summary.not_due.append(game["game_id"])
            continue
        summary.games += 1
        for kind in FEED_KINDS:
            try:
                response = api.recheck(kind, game["season"], game["game_id"], after)
            except NotFoundError:
                summary.not_found.append(f"{kind} {game['game_id']}")
                continue
            if response.cached:
                summary.reused += 1
            else:
                summary.fetched += 1
    return summary


def compare(table: str, first: pl.DataFrame, later: pl.DataFrame) -> pl.DataFrame:
    """Per field, the rows of one game's table whose value differs between its two copies; the
    rows with any differing value (CHANGED); and the rows only one copy has. Values are compared
    null-safely, and only counts above zero are listed."""
    key = KEYS[table]
    fields = [c for c in first.columns if c not in NOT_COMPARED and c not in key]
    a = first.select(*key, *fields).with_columns(_first=pl.lit(True))
    b = later.select(*key, *fields).with_columns(_later=pl.lit(True))
    joined = a.join(b, on=key, how="full", suffix="_later", coalesce=True, nulls_equal=True)
    both = joined.filter(pl.col("_first").is_not_null() & pl.col("_later").is_not_null())
    counts = {
        ADDED: joined.filter(pl.col("_first").is_null()).height,
        REMOVED: joined.filter(pl.col("_later").is_null()).height,
    }
    differs = [pl.col(name).ne_missing(pl.col(f"{name}_later")) for name in fields]
    counts[CHANGED] = both.filter(pl.any_horizontal(differs)).height if differs else 0
    for name, differ in zip(fields, differs, strict=True):
        counts[name] = both.filter(differ).height
    rows = [(table, name, rows) for name, rows in counts.items() if rows]
    return pl.DataFrame(rows, schema=CHANGE_SCHEMA, orient="row")


@dataclass(frozen=True)
class GameDiff:
    """One game's tables parsed from both copies: rows per table in the tables' copy, and each
    table's differences."""

    game_id: int
    rows: dict[str, int]
    changes: pl.DataFrame


def _copies(store: RawStore, kind: str, game: dict[str, Any]) -> tuple[str, str] | None:
    first = store.latest(f"{SOURCE}/{kind}/{_entity(game)}")
    later = store.latest(f"{SOURCE}/{kind}{RECHECK_SUFFIX}/{_entity(game)}")
    return None if first is None or later is None else (first, later)


def diff_game(store: RawStore, game: dict[str, Any]) -> GameDiff | None:
    """The game's tables from the copies they read against its recheck, or None when either copy
    of a feed is missing."""
    keys = {kind: _copies(store, kind, game) for kind in FEED_KINDS}
    if any(pair is None for pair in keys.values()):
        return None
    pairs = {kind: pair for kind, pair in keys.items() if pair is not None}
    first = parse_feeds(game, {k: (store.get(a), a) for k, (a, _) in pairs.items()})
    later = parse_feeds(game, {k: (store.get(b), b) for k, (_, b) in pairs.items()})
    changes = pl.concat(
        [pl.DataFrame(schema=CHANGE_SCHEMA)]
        + [compare(table, first[table], later[table]) for table in FEED_TABLES]
    )
    return GameDiff(game["game_id"], {t: first[t].height for t in FEED_TABLES}, changes)
