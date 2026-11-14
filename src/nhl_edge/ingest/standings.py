"""NHL standings by date (#422): every team's line of the league standings as of a past date,
from the free /v1/standings/{date} endpoint of api-web.nhle.com (docs/data-sources.md, "NHL
standings by date").

A date's standings include that date's games, so its rows are observed at 10:00 UTC the morning
after the date, when the date's results count as public (ADR 0003), and a game on date D reads
the standings of D-1 at the latest (standings_as_of). `nhl standings fetch` stores each date's
response raw (NhlApi.standings) and parses it into Standings rows, one per team, so a response
that changed shape fails the run. No lake table holds the rows yet: #422's research reads them
through fetch_standings, which reuses the stored copies, and no model reads them.
"""

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx
import polars as pl
from pandera.errors import SchemaError, SchemaErrors

from nhl_edge.ingest.games import result_public_utc
from nhl_edge.ingest.nhl_api import NhlApi, NhlApiError, NotCachedError
from nhl_edge.lake.schemas import STANDINGS_CLINCH, Standings, dtypes

# Each Standings column a team's entry gives, by its field in the entry.
COUNTS = {
    "games_played": "gamesPlayed",
    "points": "points",
    "wins": "wins",
    "losses": "losses",
    "ot_losses": "otLosses",
}
ABBREVIATIONS = {"conference": "conferenceAbbrev", "division": "divisionAbbrev"}
# A failure to fetch: the request after its retries, a copy missing in replay, or a date the
# client refuses (today's or later).
FetchErrors = (NhlApiError, NotCachedError, httpx.HTTPError, ValueError)
# A failure to parse: the response's JSON, its shape, or the rows' schema.
ParseErrors = (ValueError, KeyError, TypeError, SchemaError, SchemaErrors)


def _count(entry: dict[str, Any], name: str, where: str) -> int:
    value = entry.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{where}: {name} is {value!r}, not a whole number")
    return value


def _abbreviation(entry: dict[str, Any], name: str, where: str) -> str:
    value = entry.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: {name} is {value!r}, not a name")
    return value


def _row(entry: Any, day: date, observed_utc: datetime, raw_key: str) -> dict[str, Any]:
    """One team's Standings row. Fails on an entry without its team code or one of the fields
    read, on a standings date other than day, and on a clinch flag outside STANDINGS_CLINCH."""
    if not isinstance(entry, dict):
        raise ValueError(f"{raw_key}: a standings entry is a {type(entry).__name__}")
    abbrev = entry.get("teamAbbrev")
    team = abbrev.get("default") if isinstance(abbrev, dict) else None
    if not isinstance(team, str) or not team:
        raise ValueError(f"{raw_key}: a standings entry has no teamAbbrev.default")
    where = f"{raw_key} {team}"
    if entry.get("date") != day.isoformat():
        raise ValueError(f"{where}: the standings of {entry.get('date')!r}, not of {day}")
    clinch = entry.get("clinchIndicator")
    if clinch is not None and clinch not in STANDINGS_CLINCH:
        raise ValueError(
            f"{where}: clinchIndicator {clinch!r} is none of {', '.join(STANDINGS_CLINCH)}"
        )
    return {
        "season": _count(entry, "seasonId", where),
        "standings_date": day,
        "team": team,
        **{column: _abbreviation(entry, name, where) for column, name in ABBREVIATIONS.items()},
        **{column: _count(entry, name, where) for column, name in COUNTS.items()},
        "clinch_indicator": clinch,
        "observed_utc": observed_utc,
        "raw_key": raw_key,
    }


def parse_standings(body: bytes, day: date, raw_key: str) -> pl.DataFrame:
    """The Standings rows of one /v1/standings/{day} response, one per team, validated, each
    observed at 10:00 UTC the morning after day (games.result_public_utc). Fails (ValueError) on
    a response that is not a JSON object with a standings list of teams, an entry that lacks a
    field read or dates its standings other than day, an unknown clinch flag, and a team listed
    twice; and (SchemaError) on rows the schema refuses."""
    data = json.loads(body)
    entries = data.get("standings") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{raw_key}: not a JSON object with a standings list")
    if not entries:
        raise ValueError(f"{raw_key}: the standings list no team")
    observed_utc = result_public_utc(day)
    rows = [_row(entry, day, observed_utc, raw_key) for entry in entries]
    listed = Counter(row["team"] for row in rows)
    twice = sorted(team for team, times in listed.items() if times > 1)
    if twice:
        raise ValueError(f"{raw_key}: {', '.join(twice)} listed more than once")
    return Standings.validate(pl.DataFrame(rows, schema=dtypes(Standings)))


def _with_times(frame: pl.DataFrame, what: str, column: str) -> None:
    """Fail unless the frame has team and the column, a datetime with a time zone and no null."""
    missing = [name for name in ("team", column) if name not in frame.columns]
    if missing:
        raise ValueError(f"the {what} have no {', '.join(missing)}")
    dtype = frame.schema[column]
    if not isinstance(dtype, pl.Datetime) or dtype.time_zone is None:
        raise ValueError(f"the {what}' {column} is {dtype}, not a datetime with a time zone")
    if frame[column].null_count():
        raise ValueError(f"the {what} have a null {column}")


def standings_as_of(standings: pl.DataFrame, asks: pl.DataFrame) -> pl.DataFrame:
    """For each ask, a row with at least team and as_of_utc, the latest standings row of its
    team observed strictly before as_of_utc: the asks in their order with their columns, then
    every column of standings but team, null where the team has none by then. A row observed at
    the ask's very time is not yet known. So a game on date D predicted at 10:00 US Eastern on D
    reads the standings of D-1 (observed 10:00 UTC on D), never D's own.

    standings is Standings rows (parse_standings), or any frame derived from them with team,
    observed_utc and the columns to read, one row per team and observed_utc. Both times must be
    datetimes with a time zone. An ask column with the name of a standings column other than
    team fails, rather than being renamed, so an observed_utc read is always the standings'."""
    _with_times(asks, "asks", "as_of_utc")
    _with_times(standings, "standings", "observed_utc")
    shared = sorted(set(asks.columns) & (set(standings.columns) - {"team"}))
    if shared:
        raise ValueError(f"the asks have columns of the standings' own: {', '.join(shared)}")
    if standings.select("team", "observed_utc").is_duplicated().any():
        raise ValueError("the standings have two rows of a team observed at the same time")
    order, key = "__ask", "__as_of_utc"

    def utc(column: str) -> pl.Expr:
        return pl.col(column).dt.convert_time_zone("UTC").dt.cast_time_unit("us")

    joined = (
        asks.with_row_index(order)
        .with_columns(utc("as_of_utc").alias(key))
        .sort(key)
        .join_asof(
            standings.with_columns(utc("observed_utc")).sort("observed_utc"),
            left_on=key,
            right_on="observed_utc",
            by="team",
            strategy="backward",
            # A row observed at the ask's time is not yet known (hard rule 1).
            allow_exact_matches=False,
            check_sortedness=False,  # both sides are sorted by time above
        )
        .sort(order)
    )
    read = [column for column in standings.columns if column != "team"]
    return joined.select(*asks.columns, *read)


@dataclass
class StandingsReport:
    """What one fetch did: the parsed rows of every date that parsed (Standings); the dates
    asked for, and of them those whose response was fetched or reused from the raw cache; and
    each fetch or parse that failed."""

    standings: pl.DataFrame = field(default_factory=lambda: pl.DataFrame(schema=dtypes(Standings)))
    dates: int = 0
    fetched: int = 0
    reused: int = 0
    failed: list[str] = field(default_factory=list)

    def teams_per_date(self) -> tuple[int, int] | None:
        """The fewest and the most teams a parsed date lists, or None when none parsed."""
        if self.standings.is_empty():
            return None
        counts = self.standings.group_by("standings_date").len()["len"].to_list()
        return min(counts), max(counts)

    def clinch_flags(self) -> dict[str, int]:
        """Team-dates by clinch flag, of the flags seen, in STANDINGS_CLINCH order."""
        seen = Counter(self.standings["clinch_indicator"].drop_nulls().to_list())
        return {flag: seen[flag] for flag in STANDINGS_CLINCH if seen[flag]}


def fetch_report(api: NhlApi, days: Iterable[date]) -> StandingsReport:
    """Fetch, or reuse from the raw cache, and parse the standings of each date, once each and
    in date order, with the counts `nhl standings fetch` prints. A fetch or parse that fails is
    recorded and the rest go on; the caller fails the run after. Every response is stored raw
    before it is parsed, so a copy that no longer parses stays for a fixed parser to replay."""
    report = StandingsReport()
    frames: list[pl.DataFrame] = []
    for day in sorted(set(days)):
        report.dates += 1
        try:
            response = api.standings(day)
        except FetchErrors as exc:
            report.failed.append(f"{day}: {type(exc).__name__}: {exc}")
            continue
        if response.cached:
            report.reused += 1
        else:
            report.fetched += 1
        try:
            frames.append(parse_standings(response.body, day, response.raw_key))
        except ParseErrors as exc:
            report.failed.append(f"{day}: {type(exc).__name__}: {exc}")
    if frames:
        report.standings = pl.concat(frames)
    return report


def fetch_standings(api: NhlApi, days: Iterable[date]) -> tuple[pl.DataFrame, list[str]]:
    """The Standings rows of every date that parsed, and each date's fetch or parse that failed
    ("<date>: <error>"), as fetch_report gets them."""
    report = fetch_report(api, days)
    return report.standings, report.failed
