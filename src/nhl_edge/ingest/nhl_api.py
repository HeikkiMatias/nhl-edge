"""NHL API client (api-web.nhle.com and api.nhle.com/stats/rest; docs/data-sources.md).

One throttle covers both hosts at about 1 request per second. Every response is stored untouched
in the raw store under nhl/<kind>/<entity>/<fetch stamp> before anything parses it. A lookup first
checks the cache: the newest stored copy is reused when the caller's reuse rule accepts it, so a
backfill can stop and restart without refetching. In offline (replay) mode the client never touches
the network and fails on a cache miss.
"""

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx

from nhl_edge.lake.raw import RawStore

BASE_URL = "https://api-web.nhle.com"
STATS_URL = "https://api.nhle.com/stats/rest"
SOURCE = "nhl"
MIN_INTERVAL_S = 1.0
BACKOFF_S = (5.0, 10.0, 20.0)
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
USER_AGENT = "nhl-edge/0.1 (personal research)"
REGULAR_SEASON = 2
PLAYOFFS = 3


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

    def _get(self, url: str) -> tuple[bytes, datetime, dict[str, Any]]:
        """GET with retries on timeouts, 429 and 5xx. A 404 raises NotFoundError at once."""
        error = ""
        for attempt, backoff in enumerate((*self.backoff_s, None), start=1):
            self._throttle()
            self.requests += 1
            retry_after: float | None = None
            try:
                response = self.client.get(url)
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
                retry_after = _seconds(response.headers.get("Retry-After"))
            if backoff is None:
                break
            self.sleep(max(backoff, retry_after or 0.0))
        raise NhlApiError(f"GET {url} failed after {len(self.backoff_s) + 1} attempts: {error}")

    def fetch(self, kind: str, entity: str, url: str, reuse: Reuse) -> Response:
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
        body, fetched_utc, meta = self._get(url)
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
        path = f"/v1/gamecenter/{game_id}/play-by-play"
        return self.fetch("play-by-play", f"{season}/{game_id}", path, always)

    def boxscore(self, season: int, game_id: int) -> Response:
        path = f"/v1/gamecenter/{game_id}/boxscore"
        return self.fetch("boxscore", f"{season}/{game_id}", path, always)

    def shift_chart(self, season: int, game_id: int) -> Response:
        url = f"{STATS_URL}/en/shiftcharts?cayenneExp=gameId={game_id}"
        return self.fetch("shiftcharts", f"{season}/{game_id}", url, always)

    def roster(self, team: str, season: int, reuse: Reuse) -> Response:
        return self.fetch("roster", f"{season}/{team}", f"/v1/roster/{team}/{season}", reuse)

    def player_landing(self, player_id: int) -> Response:
        path = f"/v1/player/{player_id}/landing"
        return self.fetch("player-landing", str(player_id), path, always)


def _seconds(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None
