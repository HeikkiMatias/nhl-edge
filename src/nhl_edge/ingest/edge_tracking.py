"""NHL EDGE tracking snapshots (#424): every team's season totals to date from the free
/v1/edge/ team endpoints of api-web.nhle.com (docs/data-sources.md, "NHL EDGE snapshots").

The endpoints take a season and a game type and no date, so a response is the season total as of
its fetch, and a dated series exists only if a copy is stored every day. `nhl edge snapshot`,
run by the nightly ingest, stores each team's six endpoints raw (NhlApi.edge_team) and parses
each copy into EdgeTeamMetrics rows, one per figure, so an endpoint that changes shape fails the
night it changes. observed_utc is the fetch time, when that total was public. No lake table holds
the rows yet: a test of whether the figures add anything would build one from the raw copies,
with per-day figures from the differences of consecutive days.

Team ids come from the boxscore of each team's latest game of the season in the lake's games,
not from a list: the NHL gave Utah a new id (59 to 68) when it took the Mammoth name in 2025-26.
"""

import json
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import polars as pl
from pandera.errors import SchemaError, SchemaErrors

from nhl_edge.ingest.feeds import FeedGame
from nhl_edge.ingest.nhl_api import PLAYOFFS, REGULAR_SEASON, NhlApi, NhlApiError, Response
from nhl_edge.lake.schemas import EDGE_ENDPOINTS, EdgeTeamMetrics, dtypes

DETAIL = "team-detail"
# The blocks each endpoint answered with on 2026-11-12. A response without one of them has
# changed shape, and its parse fails.
BLOCKS = {
    "team-detail": (
        "team",
        "shotSpeed",
        "skatingSpeed",
        "distanceSkated",
        "sogSummary",
        "sogDetails",
        "zoneTimeDetails",
    ),
    "team-zone-time-details": ("zoneTimeDetails", "shotDifferential"),
    "team-skating-speed-detail": ("skatingSpeedDetails",),
    "team-skating-distance-detail": ("skatingDistanceDetails",),
    "team-shot-speed-detail": ("shotSpeedDetails",),
    "team-shot-location-detail": ("shotLocationDetails", "shotLocationTotals"),
}
# Keys whose values give no figure: the highlight of one player's event behind a team figure
# (overlay) and the top-ten lists of single players' bursts and shots, which are skater figures
# (out of scope in #424); the seasons the API has EDGE data for; and ids, which are keys.
SKIPPED = frozenset({"overlay", "topSkatingSpeeds", "hardestShots", "seasonsWithEdgeStats", "id"})
# The fields that name a list item in a metric path, in this order: the strength, location,
# position, rink area or game date the item gives the figures of.
ITEM_KEYS = ("strengthCode", "locationCode", "positionCode", "position", "area", "gameDate")
# A failure to parse: the response's JSON, its shape, or the rows' schema.
ParseErrors = (ValueError, KeyError, TypeError, AttributeError, SchemaError, SchemaErrors)


def item_name(item: Any, path: str) -> str:
    """The name of a list item in a metric path, such as strengthCode=es,positionCode=F. An item
    no field names would only have its position, which can change, so it fails the parse."""
    if not isinstance(item, dict):
        raise ValueError(f"{path} lists a {type(item).__name__}, not an object")
    named = [f"{key}={item[key]}" for key in ITEM_KEYS if isinstance(item.get(key), str)]
    if not named:
        raise ValueError(f"an item of {path} has none of the fields that name one, {ITEM_KEYS}")
    return ",".join(named)


def figures(node: Any, path: str = "") -> Iterator[tuple[str, float]]:
    """(metric, value) for every number in a response, depth first in the response's order.
    Text, booleans, nulls and the SKIPPED keys give none."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key not in SKIPPED:
                yield from figures(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for item in node:
            yield from figures(item, f"{path}[{item_name(item, path)}]")
    elif isinstance(node, int | float) and not isinstance(node, bool):
        yield path, float(node)


def parse_edge_team(
    endpoint: str,
    body: bytes,
    *,
    team: str,
    team_id: int,
    season: int,
    game_type: int,
    observed_utc: datetime,
    raw_key: str,
) -> pl.DataFrame:
    """One EdgeTeamMetrics row per figure of one EDGE team response, validated. Fails when the
    response lacks one of its endpoint's BLOCKS, gives no figure, or, for team-detail, names
    another team."""
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError(f"{raw_key}: not a JSON object")
    missing = [block for block in BLOCKS[endpoint] if block not in data]
    if missing:
        raise ValueError(f"{raw_key}: {endpoint} has no {', '.join(missing)}")
    if endpoint == DETAIL:
        named = (data["team"].get("id"), data["team"].get("abbrev"))
        if named != (team_id, team):
            raise ValueError(f"{raw_key}: team-detail of {named[1]} ({named[0]}), not {team}")
    rows = [
        {
            "team": team,
            "team_id": team_id,
            "season": season,
            "game_type": game_type,
            "endpoint": endpoint,
            "metric": metric,
            "value": value,
            "observed_utc": observed_utc,
            "raw_key": raw_key,
        }
        for metric, value in figures(data)
    ]
    if not rows:
        raise ValueError(f"{raw_key}: {endpoint} gives no figure")
    return EdgeTeamMetrics.validate(pl.DataFrame(rows, schema=dtypes(EdgeTeamMetrics)))


def edge_game_types(detail: bytes, season: int) -> tuple[int, ...]:
    """The game types a team-detail response lists EDGE data for in the season (its
    seasonsWithEdgeStats), of the regular season and playoffs only. A response without that list
    has changed shape and fails, so it is never taken for a season with no figures yet (Codex on
    #432)."""
    data = json.loads(detail)
    listed = data.get("seasonsWithEdgeStats") if isinstance(data, dict) else None
    if not isinstance(listed, list) or not all(isinstance(entry, dict) for entry in listed):
        raise ValueError("team-detail has no seasonsWithEdgeStats list")
    types = {t for entry in listed if entry.get("id") == season for t in entry.get("gameTypes", [])}
    return tuple(t for t in (REGULAR_SEASON, PLAYOFFS) if t in types)


def team_ids(
    api: NhlApi, games: pl.DataFrame, season: int, teams: Collection[str] | None = None
) -> dict[str, int]:
    """Each team's NHL id by triCode, from the boxscore of its latest game of the season in games
    (Games' columns), for the given teams or every team with a game. A team without a final game
    of the season is left out, so before the season's first night there is none. The boxscores
    are the ingest's cached copies; one is fetched only if the cache lacks it."""
    played = games.filter(pl.col("season") == season).sort("start_utc", "game_id", descending=True)
    wanted = set(played["home"]) | set(played["away"]) if teams is None else set(teams)
    found: dict[str, int] = {}
    for game in played.iter_rows(named=True):
        if wanted <= found.keys():
            break
        if not {game["home"], game["away"]} & (wanted - found.keys()):
            continue
        feed = FeedGame.from_boxscore(game, api.boxscore(season, game["game_id"]).body)
        found |= {feed.home: feed.home_id, feed.away: feed.away_id}
    return {team: found[team] for team in sorted(wanted & found.keys())}


@dataclass
class SnapshotReport:
    """What one snapshot did: teams snapshotted, and of them those with playoff figures too;
    responses stored, fetched or reused from earlier the same day, and their figures; teams the
    API lists no EDGE data for in the season yet; and each fetch or parse that failed."""

    teams: int = 0
    playoff_teams: int = 0
    responses: int = 0
    fetched: int = 0
    reused: int = 0
    figures: int = 0
    not_listed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def snapshot(api: NhlApi, teams: Mapping[str, int], season: int) -> SnapshotReport:
    """Fetch, store and parse every EDGE team endpoint of each team for the season: the regular
    season, and the playoffs too once the team's team-detail lists them for the season. A fetch
    or parse that fails is recorded and the rest go on, so one changed endpoint costs no other
    copy; the caller fails the run after."""
    report = SnapshotReport()

    def fetch(team: str, endpoint: str, game_type: int) -> Response | None:
        try:
            response = api.edge_team(endpoint, teams[team], season, game_type)
        except (NhlApiError, httpx.HTTPError) as exc:
            report.failed.append(f"{team} {endpoint} {game_type}: {exc}")
            return None
        report.responses += 1
        if response.cached:
            report.reused += 1
        else:
            report.fetched += 1
        return response

    def parse(team: str, endpoint: str, game_type: int, response: Response) -> None:
        try:
            rows = parse_edge_team(
                endpoint,
                response.body,
                team=team,
                team_id=teams[team],
                season=season,
                game_type=game_type,
                observed_utc=response.fetched_utc,
                raw_key=response.raw_key,
            )
        except ParseErrors as exc:
            report.failed.append(f"{team} {endpoint} {game_type}: {type(exc).__name__}: {exc}")
            return
        report.figures += rows.height

    for team in sorted(teams):
        detail = fetch(team, DETAIL, REGULAR_SEASON)
        if detail is None:
            continue
        try:
            game_types = edge_game_types(detail.body, season)
        except ParseErrors as exc:
            # A changed team-detail fails the run, but the day's other totals are still stored:
            # they can never be fetched again (Codex on #432).
            report.failed.append(f"{team} {DETAIL} {REGULAR_SEASON}: {exc}")
            game_types = (REGULAR_SEASON,)
        if REGULAR_SEASON not in game_types:
            report.not_listed.append(team)
            continue
        report.teams += 1
        if PLAYOFFS in game_types:
            report.playoff_teams += 1
        for game_type in game_types:
            for endpoint in EDGE_ENDPOINTS:
                first = (endpoint, game_type) == (DETAIL, REGULAR_SEASON)
                response = detail if first else fetch(team, endpoint, game_type)
                if response is not None:
                    parse(team, endpoint, game_type, response)
    return report
