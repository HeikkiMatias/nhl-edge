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
"""

import json
import re
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx
import polars as pl

from nhl_edge.ingest.nhl_api import USER_AGENT, parse_utc, utc_now
from nhl_edge.ingest.odds import team_code
from nhl_edge.lake.raw import SUFFIX, RawStore
from nhl_edge.lake.schemas import DailyFaceoffGoalies, dtypes
from nhl_edge.lake.tables import Lake

BASE_URL = "https://www.dailyfaceoff.com"
SOURCE = "dailyfaceoff"
PAGES = "starting-goalies"
PREFIX = f"{SOURCE}/{PAGES}"
TABLE = "dailyfaceoff_goalies"
MIN_INTERVAL_S = 1.0
NEXT_DATA = re.compile(rb'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class DailyFaceoffError(RuntimeError):
    """A failed or unexpected Daily Faceoff response."""


@dataclass(frozen=True)
class Page:
    body: bytes
    raw_key: str
    fetched_utc: datetime


class DailyFaceoff:
    """Fetches the starting-goalies page of an ET date and stores it raw before returning it."""

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
        if self._fetched:
            self.sleep(self.min_interval_s)
        self._fetched = True
        path = f"/{PAGES}/{day.isoformat()}"
        try:
            response = self.client.get(path)
        except httpx.HTTPError as exc:
            raise DailyFaceoffError(f"GET {path} failed: {type(exc).__name__}") from None
        fetched_utc = self.now()
        if response.status_code != 200:
            raise DailyFaceoffError(f"GET {path} returned {response.status_code}")
        raw_key = self.store.put(
            SOURCE,
            f"{PAGES}/{day.isoformat()}/{fetched_utc:%Y%m%dT%H%M%SZ}",
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
