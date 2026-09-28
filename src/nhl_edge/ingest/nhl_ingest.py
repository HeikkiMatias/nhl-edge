"""`nhl ingest`: NHL games and players into the lake, with the raw per-game feeds cached for the
shot, shift and lineup parsers.

A window is a season or a date range. For each week of it: fetch the schedule, keep the final
regular-season games, and fetch play-by-play, boxscore and shift chart for each (feeds). Then fetch
the rosters of every team that played, and a landing page for every player on them or in the
boxscores who is not in the players table yet. Games are written as whole game_date partitions,
players merged by player_id, and games upserted to Supabase. Every response goes to the raw store
first, and cached copies are reused by the rules in nhl_api, so a stopped backfill restarts where it
left off and --replay rebuilds the tables with no network at all.
"""

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl

from nhl_edge.ingest.games import (
    EXPECTED_GAMES,
    FINAL,
    listed_games,
    parse_games,
    probe_date,
    season_bounds,
    season_over,
    settled_on,
)
from nhl_edge.ingest.nhl_api import (
    NhlApi,
    NotCachedError,
    NotFoundError,
    fetched_after,
)
from nhl_edge.ingest.players import (
    boxscore_player_ids,
    landing_row,
    parse_players,
    roster_player_ids,
)
from nhl_edge.lake.schemas import Games, dtypes
from nhl_edge.lake.supabase import Supabase
from nhl_edge.lake.tables import TABLES, Lake

ET = ZoneInfo("America/New_York")
WEEK = timedelta(days=7)

Echo = Callable[[str], None]


@dataclass(frozen=True)
class Season:
    season: int


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date


Window = Season | DateRange


@dataclass
class Summary:
    label: str
    listed: int = 0
    written: int = 0
    not_final: list[tuple[int, str]] = field(default_factory=list)
    players_seen: int = 0
    players_parsed: int = 0
    players_missing: list[int] = field(default_factory=list)
    requests: int = 0
    cache_hits: int = 0


def yesterday_et(now: datetime) -> date:
    """The NHL game date that ended most recently: yesterday in US Eastern time."""
    return now.astimezone(ET).date() - timedelta(days=1)


def recent_days(now: datetime, days: int) -> "DateRange":
    """The last `days` game dates up to yesterday (US Eastern). The nightly job looks back a few
    days, so a game that was not final yet, or a missed run, is picked up the next night."""
    if days < 1:
        raise ValueError("--recent must be at least 1")
    last = yesterday_et(now)
    return DateRange(last - timedelta(days=days - 1), last)


def parse_seasons(text: str) -> list[int]:
    """Seasons as 20232024, a comma list, or an inclusive range 20102011-20252026."""
    seasons: list[int] = []
    for part in text.split(","):
        first, _, last = part.strip().partition("-")
        start, stop = _season(first), _season(last or first)
        if stop < start:
            raise ValueError(f"season range {part.strip()!r} runs backwards")
        seasons.extend(range(start, stop + 1, 10_001))
    return seasons


def _season(text: str) -> int:
    if len(text) != 8 or not text.isdigit() or int(text[4:]) != int(text[:4]) + 1:
        raise ValueError(f"expected a season like 20232024, got {text!r}")
    return int(text)


def warn(echo: Echo, message: str) -> None:
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") == "true" else "warning: "
    echo(f"{prefix}{message}")


@dataclass
class Ingest:
    api: NhlApi
    lake: Lake
    supabase: Supabase | None
    feeds: bool
    players: bool
    echo: Echo

    def run(self, windows: Iterable[Window]) -> list[Summary]:
        summaries = [self.window(window) for window in windows]
        if self.supabase is not None:
            self.supabase.ping("games", "game_id")
        return summaries

    def window(self, window: Window) -> Summary:
        api = self.api
        requests, hits = api.requests, api.cache_hits
        if isinstance(window, Season):
            label = str(window.season)
            probe = api.schedule_week(probe_date(window.season), season_over)
            start, end = season_bounds(probe.body)
        else:
            label = f"{window.start.isoformat()}..{window.end.isoformat()}"
            start, end = window.start, window.end
        summary = Summary(label)

        frames = [pl.DataFrame(schema=dtypes(Games))]
        boxscore_ids: set[int] = set()
        week = start
        while week <= end:
            days = {week + timedelta(days=i) for i in range(min(7, (end - week).days + 1))}
            response = api.schedule_week(week, settled_on(days))
            listed = listed_games(response.body, days)
            summary.listed += len(listed)
            summary.not_final += [
                (game["id"], game["gameState"]) for _, game in listed if game["gameState"] != FINAL
            ]
            games = parse_games(listed, response.raw_key)
            frames.append(games)
            if self.feeds:
                for season, game_id in games.select("season", "game_id").iter_rows():
                    api.play_by_play(season, game_id)
                    boxscore_ids |= boxscore_player_ids(api.boxscore(season, game_id).body)
                    api.shift_chart(season, game_id)
            self.echo(
                f"{label} week {week.isoformat()}: {games.height} final games, "
                f"{api.requests - requests} requests, {api.cache_hits - hits} cache hits so far"
            )
            week += WEEK

        games = pl.concat(frames)
        summary.written = games.height
        # The window's final games are the whole content of its dates, so a replay or parser fix
        # that drops a game also drops its stale partition.
        window_days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        self.lake.replace_dates("games", games, window_days)
        if games.height and self.supabase is not None:
            self.supabase.upsert("games", games, TABLES["games"].key)
        if self.players:
            self.ingest_players(games, boxscore_ids, summary)

        summary.requests, summary.cache_hits = api.requests - requests, api.cache_hits - hits
        _report(summary, window, self.echo)
        return summary

    def ingest_players(self, games: pl.DataFrame, boxscore_ids: set[int], summary: Summary) -> None:
        """Rosters of every team that played, then landing pages for players not yet in the table.
        A replay parses every player again, so a parser fix reaches old rows."""
        ids = set(boxscore_ids)
        team_games = pl.concat(
            [
                games.select("season", pl.col(side).alias("team"), "observed_utc")
                for side in ("home", "away")
            ]
        )
        last_games = team_games.group_by("season", "team").agg(pl.col("observed_utc").max())
        for season, team, last_observed in last_games.sort("season", "team").iter_rows():
            # A roster fetched after the team's last ingested game lists everyone who played in it.
            try:
                roster = self.api.roster(team, season, fetched_after(last_observed))
            except (NotFoundError, NotCachedError) as exc:
                warn(self.echo, f"roster {team} {season} unavailable, boxscores only: {exc}")
                continue
            ids |= roster_player_ids(roster.body)
        summary.players_seen = len(ids)

        self.lake.pull("players")
        known = set(self.lake.read("players")["player_id"].to_list())
        rows = []
        for player_id in sorted(ids if self.api.offline else ids - known):
            try:
                response = self.api.player_landing(player_id)
            except (NotFoundError, NotCachedError):
                summary.players_missing.append(player_id)
                continue
            rows.append(landing_row(response.body, response.fetched_utc, response.raw_key))
        summary.players_parsed = len(rows)
        if rows:
            self.lake.upsert("players", parse_players(rows))


def _report(summary: Summary, window: Window, echo: Echo) -> None:
    echo(
        f"{summary.label}: {summary.listed} regular-season games listed, {summary.written} final "
        f"written; players {summary.players_seen} seen, {summary.players_parsed} parsed; "
        f"{summary.requests} NHL requests, {summary.cache_hits} cache hits"
    )
    if summary.not_final:
        states = ", ".join(f"{game_id} {state}" for game_id, state in summary.not_final)
        warn(echo, f"{summary.label}: {len(summary.not_final)} games not final, skipped: {states}")
    if summary.players_missing:
        warn(
            echo,
            f"{summary.label}: no landing page for {len(summary.players_missing)} players: "
            f"{', '.join(map(str, summary.players_missing))}",
        )
    if isinstance(window, Season) and window.season in EXPECTED_GAMES:
        expected = EXPECTED_GAMES[window.season]
        if summary.written != expected:
            warn(echo, f"{summary.label}: {summary.written} games written, expected {expected}")
