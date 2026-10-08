"""Daily Faceoff starting goalies (#48): each team's expected starter and its status (such as
Likely or Confirmed), recorded with the NHL pre-game poll so the audit (#9) can compare how early
each source names the starter and how often it is right.

Daily Faceoff has no public API; its robots.txt allows the starting-goalies pages and disallows
/api/, which is never called. No terms of use were found (checked 2026-09-29, issue #48). The page
for an ET date, /starting-goalies/YYYY-MM-DD, embeds the day's games as JSON in its __NEXT_DATA__
script. Each fetch is stored untouched as HTML under dailyfaceoff/starting-goalies/<date>/<stamp>,
since a page seen before puck drop cannot be fetched again, and parsed from the raw copy.

A status carries Daily Faceoff's own report time (newsCreatedAt) and source, but a status can
change, so rows count as public at observed_utc, the fetch time. Goalies are kept by name and
Daily Faceoff id; matching them to NHL player ids is left to the audit, which has the players.

**Line combinations (#121).** Each team's page, /teams/<slug>/line-combinations, embeds its
projected lines, power-play and penalty-kill units and injured players as JSON, with each
player's injury status ("out", "dtd") and game-time-decision flag. The slot goalie polls fetch
the page of every team playing in their window, stored untouched under
dailyfaceoff/line-combinations/<ET game date>/<team>/<stamp>, to measure on live games whether
the statuses would improve the lineup availability model (ADR 0017). Live-only: no source dates
past statuses, so nothing here enters a backtest.
"""

import json
import re
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import polars as pl
from pandera.errors import SchemaError

from nhl_edge.ingest.nhl_api import USER_AGENT, parse_utc, utc_now
from nhl_edge.ingest.odds import team_code
from nhl_edge.lake.raw import SUFFIX, RawStore
from nhl_edge.lake.schemas import DailyFaceoffGoalies, DailyFaceoffLines, dtypes
from nhl_edge.lake.tables import Lake

BASE_URL = "https://www.dailyfaceoff.com"
SOURCE = "dailyfaceoff"
PAGES = "starting-goalies"
PREFIX = f"{SOURCE}/{PAGES}"
TABLE = "dailyfaceoff_goalies"
LINES = "line-combinations"
LINES_PREFIX = f"{SOURCE}/{LINES}"
LINES_TABLE = "dailyfaceoff_lines"
MIN_INTERVAL_S = 1.0
# The slot polls run before the odds snapshot, and at midday before the decision: the team pages
# never hold them up longer than this. Teams left when it runs out are skipped, not failed.
LINES_BUDGET = timedelta(minutes=2)
# Daily Faceoff's team page slugs, by NHL triCode.
TEAM_SLUGS = {
    "ANA": "anaheim-ducks",
    "BOS": "boston-bruins",
    "BUF": "buffalo-sabres",
    "CAR": "carolina-hurricanes",
    "CBJ": "columbus-blue-jackets",
    "CGY": "calgary-flames",
    "CHI": "chicago-blackhawks",
    "COL": "colorado-avalanche",
    "DAL": "dallas-stars",
    "DET": "detroit-red-wings",
    "EDM": "edmonton-oilers",
    "FLA": "florida-panthers",
    "LAK": "los-angeles-kings",
    "MIN": "minnesota-wild",
    "MTL": "montreal-canadiens",
    "NJD": "new-jersey-devils",
    "NSH": "nashville-predators",
    "NYI": "new-york-islanders",
    "NYR": "new-york-rangers",
    "OTT": "ottawa-senators",
    "PHI": "philadelphia-flyers",
    "PIT": "pittsburgh-penguins",
    "SEA": "seattle-kraken",
    "SJS": "san-jose-sharks",
    "STL": "st-louis-blues",
    "TBL": "tampa-bay-lightning",
    "TOR": "toronto-maple-leafs",
    "UTA": "utah-mammoth",
    "VAN": "vancouver-canucks",
    "VGK": "vegas-golden-knights",
    "WPG": "winnipeg-jets",
    "WSH": "washington-capitals",
}
NEXT_DATA = re.compile(rb'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class DailyFaceoffError(RuntimeError):
    """A failed or unexpected Daily Faceoff response."""


@dataclass(frozen=True)
class Page:
    body: bytes
    raw_key: str
    fetched_utc: datetime


class DailyFaceoff:
    """Fetches the starting-goalies page of an ET date, or a team's line-combinations page, and
    stores it raw before returning it."""

    def __init__(
        self,
        store: RawStore,
        client: httpx.Client | None = None,
        *,
        now: Callable[[], datetime] = utc_now,
        min_interval_s: float = MIN_INTERVAL_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.client = client or httpx.Client(
            base_url=BASE_URL, headers={"User-Agent": USER_AGENT}, timeout=30
        )
        self.now = now
        self.min_interval_s = min_interval_s
        self.sleep = sleep
        self._fetched = False

    def starting_goalies(self, day: date) -> Page:
        return self._get(f"/{PAGES}/{day.isoformat()}", f"{PAGES}/{day.isoformat()}")

    def line_combinations(self, team: str, game_date: date, timeout: float | None = None) -> Page:
        """The team's line-combinations page, stored under its game's ET date and the team; the
        request gives up after timeout seconds when one is given."""
        return self._get(
            f"/teams/{TEAM_SLUGS[team]}/{LINES}",
            f"{LINES}/{game_date.isoformat()}/{team}",
            timeout,
        )

    def _get(self, path: str, key: str, timeout: float | None = None) -> Page:
        if self._fetched:
            self.sleep(self.min_interval_s)
        self._fetched = True
        try:
            response = self.client.get(
                path, timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout
            )
        except httpx.HTTPError as exc:
            raise DailyFaceoffError(f"GET {path} failed: {type(exc).__name__}") from None
        fetched_utc = self.now()
        if response.status_code != 200:
            raise DailyFaceoffError(f"GET {path} returned {response.status_code}")
        raw_key = self.store.put(
            SOURCE,
            f"{key}/{fetched_utc:%Y%m%dT%H%M%SZ}",
            response.content,
            {
                "url": str(response.request.url),
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "fetched_utc": fetched_utc.isoformat(),
            },
        )
        return Page(response.content, raw_key, fetched_utc)


def season_of(day: date) -> int:
    """20262027 for any date from July 2026 to June 2027."""
    start = day.year if day.month >= 7 else day.year - 1
    return start * 10_000 + start + 1


def page_games(body: bytes) -> list[dict[str, Any]]:
    """The games embedded in a starting-goalies page."""
    match = NEXT_DATA.search(body)
    if match is None:
        raise DailyFaceoffError("no __NEXT_DATA__ script in the starting-goalies page")
    return json.loads(match.group(1))["props"]["pageProps"]["data"] or []


def parse_starting_goalies(body: bytes, observed_utc: datetime, raw_key: str) -> pl.DataFrame:
    """One row per team of every game on the page that had not started at observed_utc."""
    rows = []
    for game in page_games(body):
        start_utc = parse_utc(game["dateGmt"])
        if observed_utc >= start_utc:
            continue
        game_date = date.fromisoformat(game["date"])
        for side, is_home in (("home", True), ("away", False)):
            reported = game.get(f"{side}NewsCreatedAt")
            rows.append(
                {
                    "season": season_of(game_date),
                    "game_date": game_date,
                    "start_utc": start_utc,
                    "team": team_code(game[f"{side}TeamName"]),
                    "is_home": is_home,
                    "goalie_name": game.get(f"{side}GoalieName"),
                    "dfo_goalie_id": game.get(f"{side}GoalieId"),
                    "status": game.get(f"{side}NewsStrengthName"),
                    "reported_utc": parse_utc(reported) if reported else None,
                    "source_url": game.get(f"{side}NewsSourceUrl"),
                    "observed_utc": observed_utc,
                    "raw_key": raw_key,
                }
            )
    return DailyFaceoffGoalies.validate(pl.DataFrame(rows, schema=dtypes(DailyFaceoffGoalies)))


def run_poll(
    *, dfo: DailyFaceoff, days: Collection[date], echo: Callable[[str], None]
) -> list[date]:
    """Fetch and store the page of each ET date, and report how many teams have a status. Returns
    the dates that failed."""
    failed = []
    for day in sorted(days):
        try:
            page = dfo.starting_goalies(day)
            rows = parse_starting_goalies(page.body, page.fetched_utc, page.raw_key)
        except (DailyFaceoffError, KeyError, ValueError) as exc:
            echo(f"::warning::daily faceoff {day}: {exc}")
            failed.append(day)
            continue
        with_status = rows.filter(pl.col("status").is_not_null())
        confirmed = with_status.filter(pl.col("status") == "Confirmed").height
        echo(
            f"daily faceoff {day}: {rows.height} teams, {with_status.height} with a status, "
            f"{confirmed} confirmed"
        )
    return failed


@dataclass
class ReplayReport:
    pages: int = 0
    rows: int = 0
    incomplete: list[str] = field(default_factory=list)
    # Team pages whose team isn't the one stored under: refused, as the poll refused them.
    wrong_team: list[str] = field(default_factory=list)
    dates: list[date] = field(default_factory=list)


def replay_starting_goalies(
    store: RawStore, lake: Lake, dates: Collection[date] | None = None
) -> ReplayReport:
    """Rebuild dailyfaceoff_goalies for the given page dates (every stored date when None) from
    the raw pages. Each requested date's partition is replaced, and deleted when it has no rows.
    Never calls Daily Faceoff."""
    report = ReplayReport()
    frames = []
    root = store.base_dir / PREFIX
    day_dirs = sorted(root.iterdir()) if root.is_dir() else []
    for day_dir in day_dirs:
        try:
            day = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if dates is not None and day not in dates:
            continue
        report.dates.append(day)
        for path in sorted(day_dir.glob(f"*{SUFFIX}")):
            raw_key = path.relative_to(store.base_dir).as_posix().removesuffix(SUFFIX)
            if not (store.base_dir / f"{raw_key}.meta.json").exists():
                report.incomplete.append(raw_key)
                continue
            observed_utc = parse_utc(store.meta(raw_key)["fetched_utc"])
            frames.append(parse_starting_goalies(store.get(raw_key), observed_utc, raw_key))
            report.pages += 1
    table = pl.concat(frames) if frames else pl.DataFrame(schema=dtypes(DailyFaceoffGoalies))
    report.rows = table.height
    requested = sorted(dates) if dates is not None else report.dates
    lake.replace_dates(TABLE, table, requested)
    return report


def page_lines(body: bytes) -> dict[str, Any]:
    """The line combinations embedded in a team's page."""
    match = NEXT_DATA.search(body)
    if match is None:
        raise DailyFaceoffError("no __NEXT_DATA__ script in the line-combinations page")
    lines = json.loads(match.group(1))["props"]["pageProps"]["combinations"]
    if not lines:
        raise DailyFaceoffError("no line combinations in the page")
    return lines


def parse_line_combinations(
    body: bytes, observed_utc: datetime, raw_key: str, game_date: date
) -> pl.DataFrame:
    """One row per player and group of a team's page fetched at observed_utc for its game on
    game_date (an ET date)."""
    lines = page_lines(body)
    updated = lines.get("updatedAt")
    team = team_code(lines["teamName"])
    rows = []
    for player in lines["players"]:
        news = (player.get("latestNews") or {}).get("createdAt")
        rows.append(
            {
                "season": season_of(game_date),
                "game_date": game_date,
                "team": team,
                "lines_updated_utc": parse_utc(updated) if updated else None,
                "lines_source": lines.get("sourceName"),
                "dfo_player_id": player["playerId"],
                "player_name": player["name"],
                "jersey": player.get("jerseyNumber"),
                "position": player.get("positionIdentifier"),
                "group": player["groupIdentifier"],
                "category": player.get("categoryIdentifier"),
                "injury_status": player.get("injuryStatus"),
                "game_time_decision": bool(player.get("gameTimeDecision")),
                "news_utc": parse_utc(news) if news else None,
                "observed_utc": observed_utc,
                "raw_key": raw_key,
            }
        )
    return DailyFaceoffLines.validate(pl.DataFrame(rows, schema=dtypes(DailyFaceoffLines)))


def run_lines_poll(
    *, dfo: DailyFaceoff, teams: Collection[tuple[str, date]], echo: Callable[[str], None]
) -> list[str]:
    """Fetch and store the line-combinations page of each team playing on its ET game date
    (team, game_date), within LINES_BUDGET, and report how many players are injured or a
    game-time decision. Returns the teams that failed."""
    failed: list[str] = []
    frames = []
    skipped = 0
    deadline = dfo.now() + LINES_BUDGET
    for team, game_date in sorted(teams):
        # Each request gets only the time left, so no page outlasts the budget by more than its
        # store (one small object).
        left = (deadline - dfo.now()).total_seconds() - dfo.min_interval_s
        if left < 1:
            skipped += 1
            continue
        try:
            page = dfo.line_combinations(team, game_date, timeout=left)
            rows = parse_line_combinations(page.body, page.fetched_utc, page.raw_key, game_date)
            if set(rows["team"]) - {team}:
                raise DailyFaceoffError(f"the page is {sorted(set(rows['team']))}'s, not {team}'s")
        # A page that doesn't parse is reported and the poll goes on: the slot's snapshot follows.
        except (DailyFaceoffError, KeyError, TypeError, ValueError, SchemaError) as exc:
            echo(f"::warning::daily faceoff lines {team} {game_date}: {exc}")
            failed.append(team)
            continue
        frames.append(rows)
    found = pl.concat(frames) if frames else pl.DataFrame(schema=dtypes(DailyFaceoffLines))
    players = ["team", "dfo_player_id"]
    injured = found.filter(pl.col("injury_status").is_not_null()).unique(players).height
    doubtful = found.filter(pl.col("game_time_decision")).unique(players).height
    echo(
        f"daily faceoff lines: {len(frames)} teams, {injured} injured players, {doubtful} "
        f"game-time decisions; {len(failed)} failed"
        + (f", {skipped} skipped after {LINES_BUDGET.seconds // 60} minutes" if skipped else "")
    )
    return failed


def replay_line_combinations(
    store: RawStore, lake: Lake, dates: Collection[date] | None = None
) -> ReplayReport:
    """Rebuild dailyfaceoff_lines for the given game dates (every stored date when None) from the
    raw team pages. Each requested date's partition is replaced, and deleted when it has no rows.
    Never calls Daily Faceoff."""
    report = ReplayReport()
    frames = []
    root = store.base_dir / LINES_PREFIX
    day_dirs = sorted(root.iterdir()) if root.is_dir() else []
    for day_dir in day_dirs:
        try:
            day = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if dates is not None and day not in dates:
            continue
        report.dates.append(day)
        for path in sorted(day_dir.glob(f"*/*{SUFFIX}")):
            raw_key = path.relative_to(store.base_dir).as_posix().removesuffix(SUFFIX)
            if not (store.base_dir / f"{raw_key}.meta.json").exists():
                report.incomplete.append(raw_key)
                continue
            observed_utc = parse_utc(store.meta(raw_key)["fetched_utc"])
            rows = parse_line_combinations(store.get(raw_key), observed_utc, raw_key, day)
            # Stored under the team asked for: another team's page was refused by the poll.
            if set(rows["team"]) != {path.parent.name}:
                report.wrong_team.append(raw_key)
                continue
            frames.append(rows)
            report.pages += 1
    table = pl.concat(frames) if frames else pl.DataFrame(schema=dtypes(DailyFaceoffLines))
    report.rows = table.height
    requested = sorted(dates) if dates is not None else report.dates
    lake.replace_dates(LINES_TABLE, table, requested)
    return report
