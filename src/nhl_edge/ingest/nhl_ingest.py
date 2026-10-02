"""`nhl ingest`: NHL games, players and the per-game tables into the lake.

A window is a season or a date range. For each week of it: fetch the schedule, keep the final
regular-season games, and fetch play-by-play, boxscore and shift chart for each (feeds), parsed
into shots, shifts, actual_lineups and shift_coverage. Then fetch the rosters of every team that
played, and a landing page for every player on them or in the boxscores who is not in the players
table yet. Games, their schedule and the per-game tables are written as whole game_date
partitions, players merged by player_id, and games upserted to Supabase. Every response goes to
the raw store first, and cached copies are reused by the rules in nhl_api, so a stopped backfill
restarts where it left off and --replay rebuilds the tables with no network at all.
"""

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from nhl_edge.ingest.feeds import FeedGame
from nhl_edge.ingest.games import (
    EXPECTED_GAMES,
    FINAL,
    listed_games,
    parse_games,
    probe_date,
    schedule_of,
    season_bounds,
    season_over,
    settled_on,
)
from nhl_edge.ingest.lineups import parse_actual_lineups
from nhl_edge.ingest.nhl_api import (
    FEED_KINDS,
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
from nhl_edge.ingest.plays import parse_faceoffs, parse_penalties
from nhl_edge.ingest.shift_coverage import chart_strength, shift_coverage
from nhl_edge.ingest.shifts import parse_shifts
from nhl_edge.ingest.shots import parse_shots
from nhl_edge.ingest.strength_time import parse_strength_time
from nhl_edge.lake.schemas import Games, dtypes
from nhl_edge.lake.supabase import Supabase
from nhl_edge.lake.tables import FEED_TABLES, TABLES, Lake

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
    # Dates a replay leaves as they are, since its cached schedule lists a game there not final.
    held: list[date] = field(default_factory=list)
    players_seen: int = 0
    players_parsed: int = 0
    players_missing: list[int] = field(default_factory=list)
    requests: int = 0
    cache_hits: int = 0
    shots: int = 0
    shifts: int = 0
    shift_charts_complete: int = 0


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


def parse_feeds(
    game: dict[str, Any], feeds: Mapping[str, tuple[bytes, str]]
) -> dict[str, pl.DataFrame]:
    """A game's three feeds, each (body, raw_key) by kind in FEED_KINDS, parsed into the per-game
    tables of FEED_TABLES."""
    pbp, box, chart = (feeds[kind] for kind in FEED_KINDS)
    feed_game = FeedGame.from_boxscore(game, box[0])
    shots = parse_shots(pbp[0], feed_game, pbp[1])
    shifts, drops = parse_shifts(chart[0], feed_game, chart[1])
    lineups = parse_actual_lineups(box[0], feed_game, box[1])
    coverage = shift_coverage(feed_game, shots, shifts, lineups, drops, chart[1])
    # A complete chart supplies the skater counts, which situationCode can drift from (ADR 0009).
    # The coverage above still compares the chart with situationCode.
    complete = coverage["complete"].item()
    if complete:
        shots = chart_strength(feed_game, shots, shifts, lineups)
    on_chart = (shifts, lineups) if complete else (None, None)
    strength = parse_strength_time(pbp[0], feed_game, pbp[1], *on_chart)
    penalties = parse_penalties(pbp[0], feed_game, pbp[1])
    faceoffs = parse_faceoffs(pbp[0], feed_game, pbp[1])
    tables = (shots, shifts, lineups, coverage, strength, penalties, faceoffs)
    return dict(zip(FEED_TABLES, tables, strict=True))


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
        feed_frames = {table: [TABLES[table].empty()] for table in FEED_TABLES}
        boxscore_ids: set[int] = set()
        held: set[date] = set()
        week = start
        while week <= end:
            days = {week + timedelta(days=i) for i in range(min(7, (end - week).days + 1))}
            response = api.schedule_week(week, settled_on(days))
            listed = listed_games(response.body, days)
            summary.listed += len(listed)
            not_final = [(day, game) for day, game in listed if game["gameState"] != FINAL]
            summary.not_final += [(game["id"], game["gameState"]) for _, game in not_final]
            # A replay's schedule copy can predate the end of games an earlier run already wrote
            # as final. Rewriting their date would delete them, so a replay leaves it alone (#109).
            if api.offline:
                held |= {day for day, _ in not_final}
            games = parse_games(listed, response.raw_key)
            frames.append(games)
            if self.feeds:
                for game in games.iter_rows(named=True):
                    boxscore_ids |= self.game_feeds(game, feed_frames)
            self.echo(
                f"{label} week {week.isoformat()}: {games.height} final games, "
                f"{api.requests - requests} requests, {api.cache_hits - hits} cache hits so far"
            )
            week += WEEK

        kept = ~pl.col("game_date").is_in(sorted(held))
        games = pl.concat(frames).filter(kept)
        summary.written = games.height
        summary.held = sorted(held)
        # The window's final games are the whole content of its dates, so a replay or parser fix
        # that drops a game also drops its stale partition. A held date is neither written nor
        # cleared.
        window_days = [
            day
            for day in (start + timedelta(days=i) for i in range((end - start).days + 1))
            if day not in held
        ]
        self.lake.replace_dates("games", games, window_days)
        self.lake.replace_dates("schedule", schedule_of(games), window_days)
        if self.feeds:
            # Written only when the feeds were read, so --no-feeds leaves these tables alone.
            parsed = {table: pl.concat(parts).filter(kept) for table, parts in feed_frames.items()}
            for table, frame in parsed.items():
                self.lake.replace_dates(table, frame, window_days)
            summary.shots, summary.shifts = parsed["shots"].height, parsed["shifts"].height
            summary.shift_charts_complete = parsed["shift_coverage"].filter("complete").height
        if games.height and self.supabase is not None:
            self.supabase.upsert("games", games, TABLES["games"].key)
        if self.players:
            self.ingest_players(games, boxscore_ids, summary)

        summary.requests, summary.cache_hits = api.requests - requests, api.cache_hits - hits
        _report(summary, window, self.echo, feeds=self.feeds)
        return summary

    def game_feeds(self, game: dict[str, Any], frames: dict[str, list[pl.DataFrame]]) -> set[int]:
        """Fetch (or reuse) a game's play-by-play, boxscore and shift chart, parse them into the
        per-game tables, and return the ids of the players dressed."""
        season, game_id = game["season"], game["game_id"]
        pbp = self.api.play_by_play(season, game_id)
        box = self.api.boxscore(season, game_id)
        chart = self.api.shift_chart(season, game_id, game["game_date"])
        responses = zip(FEED_KINDS, (pbp, box, chart), strict=True)
        feeds = {kind: (response.body, response.raw_key) for kind, response in responses}
        for table, frame in parse_feeds(game, feeds).items():
            frames[table].append(frame)
        return boxscore_player_ids(box.body)

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


def _report(summary: Summary, window: Window, echo: Echo, *, feeds: bool) -> None:
    echo(
        f"{summary.label}: {summary.listed} regular-season games listed, {summary.written} final "
        f"written; players {summary.players_seen} seen, {summary.players_parsed} parsed; "
        f"{summary.requests} NHL requests, {summary.cache_hits} cache hits"
        + (
            f"; {summary.shots} shots, {summary.shifts} shifts, shift charts complete in "
            f"{summary.shift_charts_complete} of {summary.written} games"
            if feeds
            else ""
        )
    )
    if summary.not_final:
        states = ", ".join(f"{game_id} {state}" for game_id, state in summary.not_final)
        warn(echo, f"{summary.label}: {len(summary.not_final)} games not final, skipped: {states}")
    if summary.held:
        days = ", ".join(day.isoformat() for day in summary.held)
        warn(
            echo,
            f"{summary.label}: left {days} as they were: the cached schedule lists games there "
            "that are not final, so a replay would delete what an earlier run wrote for them. "
            "Replay a window whose schedule copy was fetched after they ended (#109).",
        )
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
