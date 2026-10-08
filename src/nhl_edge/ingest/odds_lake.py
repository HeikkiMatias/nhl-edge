"""Odds history in the lake (#20): every stored Odds API snapshot, replayed from the raw cache
without calling the Odds API, with each event matched to its NHL game.

Supabase keeps only a 36-hour window of quotes; the raw responses under odds/<date>/ keep every
listed game. Each is parsed with parse_odds, using its sidecar's fetch time as the snapshot time,
and written to the lake's odds_snapshots table, one partition per snapshot date. Quotes for games
already under way are left out, as in Supabase.

Events are matched against every NHL schedule listing in the raw cache (nhl/schedule/<date>/): the
odds job stores the schedule it checks on every run, and the nightly ingest one per day, with all
game types and upcoming games. An event matches the listing of the same home and away teams whose
start is nearest its commence time, within MATCH_WINDOW; the two sources can differ by minutes
(MTL at TOR on 2026-09-29: 23:00 UTC by the NHL, 23:10 by the Odds API). Every listing counts, not
only the newest, so an event priced before a postponement keeps the game it was priced for.

is_closing_proxy is derived here, in the replay itself, since the replay rebuilds the table from
the raw responses and would wipe a flag stored apart (#21, ADR 0033, market/closing.py). A game's
snapshots span two UTC dates (the midday decision snapshot, then the evening's), and a postponed
game's later snapshots give its new start, so the FLAG_CONTEXT days on either side of the
requested dates are read too, for the flag only, and never written.
"""

from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import polars as pl

from nhl_edge.ingest.nhl_api import parse_utc, scheduled_games
from nhl_edge.ingest.odds import SOURCE, parse_odds
from nhl_edge.lake.raw import SUFFIX, RawStore
from nhl_edge.lake.schemas import LakeOddsSnapshots, dtypes
from nhl_edge.lake.tables import Lake
from nhl_edge.market import closing

MATCH_WINDOW = timedelta(hours=12)
# A /v1/schedule/{date} response lists the seven days from its date.
SCHEDULE_DAYS = 7
SCHEDULE_PREFIX = "nhl/schedule"
# Days read on either side of a replay's dates to derive the closing proxy: the next date's
# snapshots, and a postponed game's later start.
FLAG_CONTEXT = 7
LISTINGS_SCHEMA = {
    "game_id": pl.Int64,
    "game_type": pl.Int8,
    "start_utc": pl.Datetime("us", "UTC"),
    "home": pl.String,
    "away": pl.String,
}
# NHL game types; others (4 is the All-Star game) are kept and reported by number.
GAME_TYPES = {1: "preseason", 2: "regular season", 3: "playoffs"}


@dataclass
class ReplayReport:
    snapshots: int = 0
    quotes: int = 0
    events: int = 0
    matched: Counter[str] = field(default_factory=Counter)
    unmatched: list[tuple[str, str, str, datetime]] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    in_play: int = 0
    dates: list[date] = field(default_factory=list)


def dated_raw_keys(root_dir: str, store: RawStore) -> dict[date, list[str]]:
    """Raw keys under <root_dir>/<YYYY-MM-DD>/, by date, complete or not."""
    root = store.base_dir / root_dir
    found: dict[date, list[str]] = {}
    if not root.is_dir():
        return found
    for day_dir in sorted(root.iterdir()):
        try:
            day = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        found[day] = sorted(
            f"{root_dir}/{day_dir.name}/{path.name.removesuffix(SUFFIX)}"
            for path in day_dir.glob(f"*{SUFFIX}")
        )
    return found


def is_complete(store: RawStore, raw_key: str) -> bool:
    return (store.base_dir / f"{raw_key}.meta.json").exists()


def nhl_listings(store: RawStore, first: date, last: date) -> pl.DataFrame:
    """Every game listed in the cached schedule responses that can cover first..last: one row per
    listing, so a game appears once for each time and date it was listed at."""
    rows = []
    for day, keys in dated_raw_keys(SCHEDULE_PREFIX, store).items():
        if not first - timedelta(days=SCHEDULE_DAYS) <= day <= last:
            continue
        for raw_key in keys:
            if not is_complete(store, raw_key):
                continue
            for game in scheduled_games(store.get(raw_key)):
                rows.append(
                    {
                        "game_id": game.game_id,
                        "game_type": game.game_type,
                        "start_utc": game.start_utc,
                        "home": game.home,
                        "away": game.away,
                    }
                )
    return pl.DataFrame(rows, schema=LISTINGS_SCHEMA).unique()


def match_games(quotes: pl.DataFrame, listings: pl.DataFrame) -> pl.DataFrame:
    """The quotes with game_id and game_type of the listing that matches each event: same home and
    away, the start nearest the commence time and within MATCH_WINDOW. Null where none does."""
    events = quotes.select("event_id", "home", "away", "commence_time_utc").unique()
    gap = (pl.col("start_utc") - pl.col("commence_time_utc")).abs()
    best = (
        events.join(listings, on=["home", "away"])
        .with_columns(gap=gap)
        .filter(pl.col("gap") <= MATCH_WINDOW)
        .sort("gap", "game_id")
        .group_by("event_id", maintain_order=True)
        .first()
        .select("event_id", "game_id", "game_type")
    )
    return quotes.join(best, on="event_id", how="left")


def replay_odds(
    store: RawStore,
    lake: Lake,
    dates: Collection[date] | None = None,
    now: datetime | None = None,
) -> ReplayReport:
    """Parse the stored snapshots of the given UTC dates (all stored dates when None) into the
    lake's odds_snapshots, with each game started by now (the clock by default) marked at its
    closing proxy. Every requested date's partition is replaced, and deleted when the date now has
    no quotes, so a parser fix leaves nothing stale. Never calls the Odds API."""
    report = ReplayReport()
    now = now or datetime.now(UTC)
    stored = dated_raw_keys(SOURCE, store)
    wanted = set(stored) if dates is None else set(dates)
    # The dates around the requested ones hold the rest of their games' snapshots.
    span = range(-FLAG_CONTEXT, FLAG_CONTEXT + 1)
    near = {day + timedelta(days=d) for day in wanted for d in span} - wanted
    frames = []
    for day, keys in stored.items():
        if day not in wanted and day not in near:
            continue
        for raw_key in keys:
            if not is_complete(store, raw_key):
                if day in wanted:
                    report.incomplete.append(raw_key)
                continue
            meta = store.meta(raw_key)
            snapshot_utc = parse_utc(meta["fetched_utc"])
            slot = str(meta.get("slot") or "unknown")
            frames.append(parse_odds(store.get(raw_key), snapshot_utc, slot, raw_key))
            report.snapshots += day in wanted
        if day in wanted:
            report.dates.append(day)
    columns = list(dtypes(LakeOddsSnapshots))
    requested = sorted(dates) if dates is not None else report.dates
    empty = pl.DataFrame(schema=dtypes(LakeOddsSnapshots))
    if not frames:
        lake.replace_dates("odds_snapshots", empty, requested)
        return report
    quotes = pl.concat(frames)
    # The Odds API also lists games under way, with live prices that move with the score. The
    # history keeps pre-game quotes only, as Supabase does, so no closing proxy can pick one up;
    # the raw responses keep everything.
    pre_game = pl.col("commence_time_utc") > pl.col("snapshot_utc")
    written = pl.col("snapshot_utc").dt.date().is_in(sorted(wanted))
    report.in_play = quotes.filter(~pre_game, written).height
    quotes = closing.flag(quotes.filter(pre_game), now).filter(written)
    if quotes.is_empty():
        lake.replace_dates("odds_snapshots", empty, requested)
        return report
    commence = pl.col("commence_time_utc").dt.date()
    first, last = quotes.select(commence.min().alias("first"), commence.max().alias("last")).row(0)
    listings = nhl_listings(store, first - timedelta(days=1), last)
    table = match_games(quotes, listings).with_columns(
        snapshot_date=pl.col("snapshot_utc").dt.date()
    )
    table = LakeOddsSnapshots.validate(table.select(columns))
    lake.replace_dates("odds_snapshots", table, requested)

    events = table.select("event_id", "home", "away", "commence_time_utc", "game_type").unique(
        "event_id", keep="first", maintain_order=True
    )
    report.quotes, report.events = table.height, events.height
    for event_id, home, away, commence, game_type in events.iter_rows():
        if game_type is None:
            report.unmatched.append((event_id, home, away, commence))
        else:
            report.matched[GAME_TYPES.get(game_type, f"game type {game_type}")] += 1
    return report
