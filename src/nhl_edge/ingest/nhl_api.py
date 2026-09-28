"""NHL API client (api-web.nhle.com): throttled to about 1 request per second, every response cached
raw. Only the schedule for now; #4 adds games, boxscores, shifts and rosters."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx

from nhl_edge.lake.raw import RawStore

BASE_URL = "https://api-web.nhle.com"
SOURCE = "nhl"
MIN_INTERVAL_S = 1.0
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


class NhlApi:
    def __init__(
        self,
        store: RawStore,
        client: httpx.Client | None = None,
        *,
        min_interval_s: float = MIN_INTERVAL_S,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.client = client or httpx.Client(
            base_url=BASE_URL, headers={"User-Agent": USER_AGENT}, timeout=30
        )
        self.min_interval_s = min_interval_s
        self.now = now
        self._last_request = float("-inf")

    def _get(self, path: str) -> tuple[bytes, datetime, dict[str, object]]:
        wait = self._last_request + self.min_interval_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()
        response = self.client.get(path)
        response.raise_for_status()
        fetched_utc = self.now()
        meta: dict[str, object] = {
            "url": str(response.request.url),
            "status": response.status_code,
            "fetched_utc": fetched_utc.isoformat(),
        }
        return response.content, fetched_utc, meta

    def schedule(self, day: date) -> list[ScheduledGame]:
        body, fetched_utc, meta = self._get(f"/v1/schedule/{day.isoformat()}")
        self.store.put(
            SOURCE, f"schedule/{day.isoformat()}/{fetched_utc:%Y%m%dT%H%M%SZ}", body, meta
        )
        return scheduled_games(body)
