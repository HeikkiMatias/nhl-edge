"""NHL API client (api-web.nhle.com and api.nhle.com/stats/rest; docs/data-sources.md).

One throttle covers both hosts at about 1 request per second. Every response is stored untouched
in the raw store under nhl/<kind>/<entity>/<fetch stamp> before anything parses it. A lookup first
checks the cache: the newest stored copy is reused when the caller's reuse rule accepts it, so a
backfill can stop and restart without refetching. In offline (replay) mode the client never touches
the network and fails on a cache miss.

The same client fetches the NHL's HTML time-on-ice reports on www.nhl.com (#68), for the games
whose shift chart is empty, once only and by `nhl toi-reports` alone (ingest/toi_reports.py).
"""

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import PERIOD_S

BASE_URL = "https://api-web.nhle.com"
STATS_URL = "https://api.nhle.com/stats/rest"
REPORTS_URL = "https://www.nhl.com/scores/htmlreports"
SOURCE = "nhl"
MIN_INTERVAL_S = 1.0
BACKOFF_S = (5.0, 10.0, 20.0)
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
USER_AGENT = "nhl-edge/0.1 (personal research)"
# For web pages that refuse a non-browser client: the SBR archive answers 404 to the httpx default.
# The NHL's report pages are asked for the same way, as a request with "Mozilla/5.0" was served.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
REGULAR_SEASON = 2
PLAYOFFS = 3
# Shift chart rows of this type are shifts; type 505 rows are goal markers.
SHIFT = 517
# A shift chart fetched this long after its game counts as settled: a gap left then is the source's.
# It matches the nightly lookback (nhl ingest --recent 3 in .github/workflows/ingest-nightly.yml),
# so every unsettled chart is refetched by some nightly run before it settles.
CHART_SETTLED = timedelta(days=3)
# A team's valid shifts in each of periods 1 to 3 must add up to at least four players' full period:
# less means the chart is cut short, even when every period has some rows.
MIN_TEAM_PERIOD_S = 4 * PERIOD_S
# The per-game feeds, by raw kind. A recheck (#30) stores a later copy under <kind>-recheck, which
# the per-game tables never read.
FEED_KINDS = ("play-by-play", "boxscore", "shiftcharts")
RECHECK_SUFFIX = "-recheck"
# A team's time-on-ice report by side, its raw kind and the letter in the page's name (#68).
TOI_KINDS = {"home": "toi-home", "visitor": "toi-visitor"}
TOI_LETTERS = {"home": "H", "visitor": "V"}


@dataclass(frozen=True)
class ScheduledGame:
    game_id: int
    game_type: int
    start_utc: datetime
    home: str
    away: str


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def clock_s(text: str | None) -> int | None:
    """Seconds in an "MM:SS" clock, or None when the value is missing or malformed."""
    if not text:
        return None
    minutes, sep, seconds = text.partition(":")
    if not sep or not minutes.isdigit() or not seconds.isdigit():
        return None
    return int(minutes) * 60 + int(seconds)


def scheduled_games(body: bytes) -> list[ScheduledGame]:
    """Every game in a /v1/schedule/{date} response, which covers the week from that date."""
    return [
        ScheduledGame(
            game_id=game["id"],
            game_type=game["gameType"],
            start_utc=parse_utc(game["startTimeUTC"]),
            home=game["homeTeam"]["abbrev"],
            away=game["awayTeam"]["abbrev"],
        )
        for day in json.loads(body)["gameWeek"]
        for game in day["games"]
    ]


class NhlApiError(RuntimeError):
    """A request that still failed after the retries."""


class NotFoundError(NhlApiError):
    """The API answered 404."""


class NotCachedError(RuntimeError):
    """Replay needed a response that is not in the raw cache."""


@dataclass(frozen=True)
class Response:
    body: bytes
    raw_key: str
    fetched_utc: datetime
    cached: bool


# Decides whether a cached copy is still good, given its body and meta sidecar.
Reuse = Callable[[bytes, dict[str, Any]], bool]


def always(body: bytes, meta: dict[str, Any]) -> bool:
    return True


def never(body: bytes, meta: dict[str, Any]) -> bool:
    return False


def chart_complete_or_settled(game_id: int, game_date: date) -> Reuse:
    """Reuse a cached shift chart when two teams each have valid shifts of this game adding up
    to MIN_TEAM_PERIOD_S in every one of periods 1 to 3, or when it was fetched CHART_SETTLED or
    more after the game. The NHL sometimes publishes a chart late or cut short, so the nightly
    lookback fetches such a copy again; once settled, a gap is taken as the source's."""
    settled = datetime(game_date.year, game_date.month, game_date.day, tzinfo=UTC) + CHART_SETTLED

    def reuse(body: bytes, meta: dict[str, Any]) -> bool:
        if parse_utc(meta["fetched_utc"]) >= settled:
            return True
        seconds: dict[tuple[Any, Any], int] = {}
        for row in json.loads(body).get("data") or []:
            start, end = clock_s(row.get("startTime")), clock_s(row.get("endTime"))
            if (
                row.get("typeCode") != SHIFT
                or row.get("gameId") != game_id
                or not row.get("playerId")
                or start is None
                or end is None
                or end <= start
            ):
                continue
            key = (row.get("teamId"), row.get("period"))
            seconds[key] = seconds.get(key, 0) + end - start
        teams = {team for team, _ in seconds}
        full = [
            t for t in teams if all(seconds.get((t, p), 0) >= MIN_TEAM_PERIOD_S for p in (1, 2, 3))
        ]
        return len(full) >= 2

    return reuse


def feed_url(kind: str, game_id: int) -> str:
    """The URL of one of a game's per-game feeds (FEED_KINDS)."""
    if kind == "shiftcharts":
        return f"{STATS_URL}/en/shiftcharts?cayenneExp=gameId={game_id}"
    if kind in ("play-by-play", "boxscore"):
        return f"/v1/gamecenter/{game_id}/{kind}"
    raise ValueError(f"{kind} is not a per-game feed")


def toi_report_url(season: int, game_id: int, side: str) -> str:
    """The URL of one team's time-on-ice report: TH for the home team, TV for the visitors, then
    the last six digits of the game id, in the season's folder."""
    return f"{REPORTS_URL}/{season}/T{TOI_LETTERS[side]}{game_id % 1_000_000:06d}.HTM"


def fetched_after(moment: datetime) -> Reuse:
    def reuse(body: bytes, meta: dict[str, Any]) -> bool:
        return parse_utc(meta["fetched_utc"]) >= moment

    return reuse


class NhlApi:
    def __init__(
        self,
        store: RawStore,
        client: httpx.Client | None = None,
        *,
        min_interval_s: float = MIN_INTERVAL_S,
        backoff_s: Sequence[float] = BACKOFF_S,
        now: Callable[[], datetime] = utc_now,
        offline: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.client = client or httpx.Client(
            base_url=BASE_URL, headers={"User-Agent": USER_AGENT}, timeout=30
        )
        self.min_interval_s = min_interval_s
        self.backoff_s = tuple(backoff_s)
        self.now = now
        self.offline = offline
        self.sleep = sleep
        self.monotonic = monotonic
        self._last_request = float("-inf")
        self.requests = 0
        self.cache_hits = 0

    def _throttle(self) -> None:
        wait = self._last_request + self.min_interval_s - self.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._last_request = self.monotonic()

    def _get(
        self, url: str, headers: Mapping[str, str] | None = None
    ) -> tuple[bytes, datetime, dict[str, Any]]:
        """GET with retries on timeouts, 429 and 5xx. A 404 raises NotFoundError at once. headers
        go with this request only, over the client's own."""
        error = ""
        for attempt, backoff in enumerate((*self.backoff_s, None), start=1):
            self._throttle()
            self.requests += 1
            retry_after: float | None = None
            try:
                response = self.client.get(url, headers=headers)
            except httpx.TransportError as exc:
                error = type(exc).__name__
            else:
                if response.status_code == 404:
                    raise NotFoundError(f"GET {url} returned 404")
                if response.status_code not in RETRY_STATUS:
                    response.raise_for_status()
                    fetched_utc = self.now()
                    meta: dict[str, Any] = {
                        "url": str(response.request.url),
                        "status": response.status_code,
                        "fetched_utc": fetched_utc.isoformat(),
                        "attempts": attempt,
                    }
                    return response.content, fetched_utc, meta
                error = f"status {response.status_code}"
                retry_after = retry_after_s(response.headers.get("Retry-After"), self.now())
            if backoff is None:
                break
            self.sleep(max(backoff, retry_after or 0.0))
        raise NhlApiError(f"GET {url} failed after {len(self.backoff_s) + 1} attempts: {error}")

    def fetch(
        self,
        kind: str,
        entity: str,
        url: str,
        reuse: Reuse,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """The newest cached copy when reuse accepts it (or in replay), otherwise a fresh GET that
        is stored raw before it is returned."""
        prefix = f"{SOURCE}/{kind}/{entity}"
        cached = self.store.latest(prefix)
        if cached is not None:
            body, meta = self.store.get(cached), self.store.meta(cached)
            if self.offline or reuse(body, meta):
                self.cache_hits += 1
                return Response(body, cached, parse_utc(meta["fetched_utc"]), cached=True)
        if self.offline:
            raise NotCachedError(f"{prefix} is not in the raw cache; run without --replay")
        body, fetched_utc, meta = self._get(url, headers)
        raw_key = self.store.put(
            SOURCE, f"{kind}/{entity}/{fetched_utc:%Y%m%dT%H%M%SZ}", body, meta
        )
        return Response(body, raw_key, fetched_utc, cached=False)

    def schedule(self, day: date) -> list[ScheduledGame]:
        """The week from day, always fetched fresh (the odds job needs today's start times)."""
        return scheduled_games(self.schedule_week(day, never).body)

    def schedule_week(self, day: date, reuse: Reuse) -> Response:
        return self.fetch("schedule", day.isoformat(), f"/v1/schedule/{day.isoformat()}", reuse)

    def play_by_play(self, season: int, game_id: int) -> Response:
        url = feed_url("play-by-play", game_id)
        return self.fetch("play-by-play", f"{season}/{game_id}", url, always)

    def boxscore(self, season: int, game_id: int) -> Response:
        return self.fetch("boxscore", f"{season}/{game_id}", feed_url("boxscore", game_id), always)

    def shift_chart(self, season: int, game_id: int, game_date: date) -> Response:
        reuse = chart_complete_or_settled(game_id, game_date)
        url = feed_url("shiftcharts", game_id)
        return self.fetch("shiftcharts", f"{season}/{game_id}", url, reuse)

    def recheck(self, kind: str, season: int, game_id: int, after: datetime) -> Response:
        """A later copy of one of a game's feeds, fetched at or after `after` (#30). It is stored
        under <kind>-recheck, so the per-game tables, which read the copies under <kind>, never
        parse it: they keep the copy the nightly ingest read, as live does (ADR 0004)."""
        url = feed_url(kind, game_id)
        return self.fetch(
            f"{kind}{RECHECK_SUFFIX}", f"{season}/{game_id}", url, fetched_after(after)
        )

    def toi_report(self, season: int, game_id: int, side: str) -> Response:
        """One team's HTML time-on-ice report (#68), side "home" or "visitor", stored under
        nhl/toi-<side>/ and always reused: the owner allowed one fetch of each page."""
        url = toi_report_url(season, game_id, side)
        headers = {"User-Agent": BROWSER_USER_AGENT}
        return self.fetch(TOI_KINDS[side], f"{season}/{game_id}", url, always, headers)

    def roster(self, team: str, season: int, reuse: Reuse) -> Response:
        return self.fetch("roster", f"{season}/{team}", f"/v1/roster/{team}/{season}", reuse)

    def player_landing(self, player_id: int) -> Response:
        path = f"/v1/player/{player_id}/landing"
        return self.fetch("player-landing", str(player_id), path, always)


def retry_after_s(value: str | None, now: datetime) -> float | None:
    """A Retry-After header in seconds: either delay-seconds or an HTTP date (RFC 9110)."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - now).total_seconds())
