"""Post-game corrections in the per-game feeds (#30, ADR 0004).

The per-game tables keep the play-by-play and boxscore the nightly ingest first fetched, and the
shift chart its lookback settled on, as live does. The 2010-26 backfill read copies fetched years
later, corrections included, so the backtest can see a corrected value that live would not have
had yet. To measure how often that happens, each final game's three feeds are fetched again
RECHECK_AFTER after the tables' copy and stored apart, under <kind>-recheck (NhlApi.recheck), where
no table reads them. Both copies go through the ingest's own parsers (nhl_ingest.parse_feeds) in
parse_copies, and diff_game compares the parsed tables row by row. The corrections replay
(audit/corrections_replay.py, #173) rebuilds the season's inputs from the later copies.

A rechecked shift chart without rows, when the tables' copy has shifts, is a failed fetch, not a
copy with every shift removed (#237): the NHL answers an empty chart for games it doesn't serve
a chart for. The recheck tries it again on later runs, and the diff compares the game's other two
feeds with the tables' chart on both sides, marking its chart as not rechecked.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import polars as pl

from nhl_edge.backtest.seasons import FIRST_LIVE_SEASON
from nhl_edge.ingest.nhl_api import (
    CHART_SETTLED,
    FEED_KINDS,
    RECHECK_SUFFIX,
    SOURCE,
    NhlApi,
    NotFoundError,
    has_shifts,
    parse_utc,
)
from nhl_edge.ingest.nhl_ingest import parse_feeds
from nhl_edge.ingest.toi_reports import CachedReports
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.tables import FEED_TABLES

# A recheck counts once fetched this long after the play-by-play the tables read.
RECHECK_AFTER = timedelta(days=7)
CHART = "shiftcharts"
# The rows of each per-game table, within a game.
KEYS: dict[str, list[str]] = {
    "shots": ["event_id"],
    "shifts": ["player_id", "period", "shift_number"],
    "actual_lineups": ["player_id"],
    "shift_coverage": ["game_id"],
    "strength_time": ["team", "strength", "own_net_empty", "opp_net_empty", "strength_source"],
    "penalties": ["event_id"],
    "faceoffs": ["event_id"],
}
# Columns that record which copy a row came from and when it counts as public, not what it says,
# and the game's own keys, which both copies share.
NOT_COMPARED = frozenset({"raw_key", "observed_utc", "game_id", "season", "game_date"})
ADDED, REMOVED, CHANGED = "row added", "row removed", "any value"
# A goal credited to another shooter, apart from a changed shooter of a missed or saved shot.
GOAL_SCORER = "goal scorer"
CHANGE_SCHEMA = {"table": pl.String, "field": pl.String, "rows": pl.Int64}
STAMP = "%Y%m%dT%H%M%SZ"


def due_window(now: datetime, days: int) -> tuple[datetime, datetime]:
    """The boxscore fetch times of the games whose recheck can fall due in the last `days` days by
    now: from RECHECK_AFTER before now back `days` days, and CHART_SETTLED more, since an
    incomplete shift chart is fetched again on later nights and its newer copy sets the date."""
    last = now - RECHECK_AFTER
    return last - timedelta(days=days) - CHART_SETTLED, last


def first_fetches(lineups: pl.DataFrame) -> pl.DataFrame:
    """Each game's game_id and the fetch time of the boxscore its tables read, from the stamp that
    ends actual_lineups' raw_key. The ingest fetches a game's three feeds together, so this dates
    the copy a recheck is measured against, whatever night the game was ingested."""
    return (
        lineups.group_by("game_id")
        .agg(pl.col("raw_key").first())
        .select(
            "game_id",
            fetched_utc=pl.col("raw_key")
            .str.split("/")
            .list.last()
            .str.strptime(pl.Datetime("us", "UTC"), STAMP),
        )
    )


def _entity(game: dict[str, Any]) -> str:
    return f"{game['season']}/{game['game_id']}"


def recheck_after(store: RawStore, game: dict[str, Any]) -> datetime | None:
    """When the game's recheck falls due: RECHECK_AFTER after the newest of the copies its tables
    read, which is the shift chart when the nightly lookback fetched an incomplete one again. None
    when a copy is missing."""
    fetched = []
    for kind in FEED_KINDS:
        key = store.latest(f"{SOURCE}/{kind}/{_entity(game)}")
        if key is None:
            return None
        fetched.append(parse_utc(store.meta(key)["fetched_utc"]))
    return max(fetched) + RECHECK_AFTER


@dataclass
class RecheckSummary:
    window: tuple[datetime, datetime]
    games: int = 0
    fetched: int = 0
    reused: int = 0
    not_due: list[int] = field(default_factory=list)
    never_ingested: list[int] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    # Games whose rechecked shift chart came back without rows while the tables' copy has shifts.
    empty_charts: list[int] = field(default_factory=list)


def recheck(
    api: NhlApi,
    games: pl.DataFrame,
    fetched: pl.DataFrame,
    now: datetime,
    days: int,
    first_season: int = FIRST_LIVE_SEASON,
) -> RecheckSummary:
    """Fetch the three feeds again for each final game from first_season on whose boxscore was
    fetched in the due window (first_fetches gives `fetched`), once the newest copy its tables
    read is RECHECK_AFTER old (recheck_after). Candidates are chosen by fetch time, not game
    date, so a game ingested nights late is still rechecked a week after. Only live seasons by
    default: the 2010-26 backfill fetched every earlier game in September 2026, and those copies
    are the late ones already. A rerun reuses a recheck it already has."""
    summary = RecheckSummary(due_window(now, days))
    start, end = summary.window
    due = (
        games.filter(pl.col("season") >= first_season)
        .join(fetched, on="game_id")
        .filter(pl.col("fetched_utc") > start, pl.col("fetched_utc") <= end)
        .sort("game_date", "game_id")
    )
    for game in due.iter_rows(named=True):
        after = recheck_after(api.store, game)
        if after is None:
            summary.never_ingested.append(game["game_id"])
            continue
        if after > now:
            summary.not_due.append(game["game_id"])
            continue
        summary.games += 1
        for kind in FEED_KINDS:
            listed = kind == CHART and _first_has_shifts(api.store, game)
            try:
                response = api.recheck(kind, game["season"], game["game_id"], after, listed=listed)
            except NotFoundError:
                summary.not_found.append(f"{kind} {game['game_id']}")
                continue
            if response.cached:
                summary.reused += 1
            else:
                summary.fetched += 1
            if listed and not has_shifts(response.body):
                summary.empty_charts.append(game["game_id"])
    return summary


def _first_has_shifts(store: RawStore, game: dict[str, Any]) -> bool:
    key = store.latest(f"{SOURCE}/{CHART}/{_entity(game)}")
    return key is not None and has_shifts(store.get(key))


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
        if table == "shots" and name == "shooter_id":
            # A goal in both copies credited to another shooter is a scoring change.
            goal = pl.col("is_goal") & pl.col("is_goal_later")
            counts[GOAL_SCORER] = both.filter(differ & goal).height
            counts[name] = both.filter(differ & ~goal).height
        else:
            counts[name] = both.filter(differ).height
    rows = [(table, name, rows) for name, rows in counts.items() if rows]
    return pl.DataFrame(rows, schema=CHANGE_SCHEMA, orient="row")


@dataclass(frozen=True)
class GameDiff:
    """One game's tables parsed from both copies: rows per table in the tables' copy, and each
    table's differences. chart_rechecked is False when the rechecked shift chart had no rows
    while the tables' copy has shifts: the tables' chart then stands on both sides (#237)."""

    game_id: int
    rows: dict[str, int]
    changes: pl.DataFrame
    chart_rechecked: bool = True


@dataclass(frozen=True)
class Copies:
    """One game's tables of FEED_TABLES parsed from the copies they read (first) and from its
    recheck (later), with chart_rechecked as in GameDiff."""

    first: dict[str, pl.DataFrame]
    later: dict[str, pl.DataFrame]
    chart_rechecked: bool = True


def _copies(store: RawStore, kind: str, game: dict[str, Any]) -> tuple[str, str] | None:
    first = store.latest(f"{SOURCE}/{kind}/{_entity(game)}")
    later = store.latest(f"{SOURCE}/{kind}{RECHECK_SUFFIX}/{_entity(game)}")
    return None if first is None or later is None else (first, later)


def parse_copies(
    store: RawStore, game: dict[str, Any], toi_reports: CachedReports | None = None
) -> Copies | None:
    """The game's tables from the copies they read and from its recheck, both through the
    ingest's parser (toi_reports as the ingest passes it), or None when either copy of a feed is
    missing."""
    keys = {kind: _copies(store, kind, game) for kind in FEED_KINDS}
    if any(pair is None for pair in keys.values()):
        return None
    pairs = {kind: pair for kind, pair in keys.items() if pair is not None}
    chart_first, chart_later = pairs[CHART]
    chart_rechecked = has_shifts(store.get(chart_later)) or not has_shifts(store.get(chart_first))
    if not chart_rechecked:
        pairs[CHART] = (chart_first, chart_first)
    first = parse_feeds(game, {k: (store.get(a), a) for k, (a, _) in pairs.items()}, toi_reports)
    later = parse_feeds(game, {k: (store.get(b), b) for k, (_, b) in pairs.items()}, toi_reports)
    return Copies(first, later, chart_rechecked)


def diff_game(store: RawStore, game: dict[str, Any]) -> GameDiff | None:
    """The game's tables from the copies they read against its recheck, or None when either copy
    of a feed is missing."""
    copies = parse_copies(store, game)
    if copies is None:
        return None
    changes = pl.concat(
        [pl.DataFrame(schema=CHANGE_SCHEMA)]
        + [compare(table, copies.first[table], copies.later[table]) for table in FEED_TABLES]
    )
    rows = {t: copies.first[t].height for t in FEED_TABLES}
    return GameDiff(game["game_id"], rows, changes, copies.chart_rechecked)
