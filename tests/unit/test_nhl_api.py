from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from nhl_edge.ingest import nhl_api
from nhl_edge.ingest.nhl_api import (
    NhlApi,
    NhlApiError,
    NotCachedError,
    NotFoundError,
    always,
    fetched_after,
    never,
)
from nhl_edge.lake.raw import RawStore

FIXTURES = Path(__file__).parent / "fixtures" / "nhl_api"
WEEK = (FIXTURES / "schedule_2010-10-07.json").read_bytes()
DAY = date(2010, 10, 7)
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

Handler = Callable[[httpx.Request], httpx.Response]


class Clock:
    """Fake monotonic clock whose sleeps are recorded and advance time."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def ticker(start: datetime = NOW) -> Callable[[], datetime]:
    """A UTC clock one second further on every call, so each fetch gets its own stamp."""
    calls = iter(range(10_000))
    return lambda: start + timedelta(seconds=next(calls))


def make_api(
    store: RawStore,
    handler: Handler,
    *,
    clock: Clock | None = None,
    min_interval_s: float = 0.0,
    offline: bool = False,
) -> NhlApi:
    clock = clock or Clock()
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=nhl_api.BASE_URL)
    return NhlApi(
        store,
        client,
        min_interval_s=min_interval_s,
        now=ticker(),
        offline=offline,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


def serve(body: bytes = WEEK) -> tuple[list[httpx.Request], Handler]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=body)

    return requests, handler


def fail(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no request expected, got {request.url}")


def sequence(*responses: httpx.Response | Exception) -> tuple[list[httpx.Request], Handler]:
    requests: list[httpx.Request] = []
    pending = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = pending.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    return requests, handler


# Throttle and retries


def test_one_throttle_covers_both_hosts(tmp_path: Path) -> None:
    clock = Clock()
    requests, handler = serve()
    api = make_api(RawStore(tmp_path), handler, clock=clock, min_interval_s=1.0)
    api.schedule_week(DAY, never)
    api.shift_chart(20102011, 2010020003)
    assert [r.url.host for r in requests] == ["api-web.nhle.com", "api.nhle.com"]
    assert clock.sleeps == [1.0]


def test_retries_503_and_429_with_backoff_and_retry_after(tmp_path: Path) -> None:
    clock = Clock()
    requests, handler = sequence(
        httpx.Response(503),
        httpx.Response(429, headers={"Retry-After": "30"}),
        httpx.Response(200, content=WEEK),
    )
    store = RawStore(tmp_path)
    response = make_api(store, handler, clock=clock).schedule_week(DAY, never)
    assert len(requests) == 3
    assert clock.sleeps == [5.0, 30.0]
    assert store.meta(response.raw_key)["attempts"] == 3
    assert response.body == WEEK


def test_retries_transport_errors(tmp_path: Path) -> None:
    requests, handler = sequence(httpx.ConnectTimeout("slow"), httpx.Response(200, content=WEEK))
    response = make_api(RawStore(tmp_path), handler).schedule_week(DAY, never)
    assert len(requests) == 2
    assert response.body == WEEK


def test_gives_up_after_four_attempts_and_stores_nothing(tmp_path: Path) -> None:
    requests, handler = sequence(*[httpx.Response(502)] * 4)
    with pytest.raises(NhlApiError, match="after 4 attempts: status 502"):
        make_api(RawStore(tmp_path), handler).schedule_week(DAY, never)
    assert len(requests) == 4
    assert not list(tmp_path.rglob("*.json.gz"))


def test_404_is_not_retried(tmp_path: Path) -> None:
    requests, handler = sequence(httpx.Response(404))
    with pytest.raises(NotFoundError):
        make_api(RawStore(tmp_path), handler).player_landing(8400000)
    assert len(requests) == 1


# Raw cache and reuse rules


def test_raw_keys_and_urls_follow_the_layout(tmp_path: Path) -> None:
    requests, handler = serve()
    store = RawStore(tmp_path)
    api = make_api(store, handler)
    keys = [
        api.schedule_week(DAY, never).raw_key,
        api.play_by_play(20102011, 2010020003).raw_key,
        api.boxscore(20102011, 2010020003).raw_key,
        api.shift_chart(20102011, 2010020003).raw_key,
        api.roster("PHX", 20102011, never).raw_key,
        api.player_landing(8478402).raw_key,
    ]
    assert [key.rsplit("/", 1)[0] for key in keys] == [
        "nhl/schedule/2010-10-07",
        "nhl/play-by-play/20102011/2010020003",
        "nhl/boxscore/20102011/2010020003",
        "nhl/shiftcharts/20102011/2010020003",
        "nhl/roster/20102011/PHX",
        "nhl/player-landing/8478402",
    ]
    assert keys[0].endswith("/20260928T120000Z")
    assert [str(r.url) for r in requests] == [
        "https://api-web.nhle.com/v1/schedule/2010-10-07",
        "https://api-web.nhle.com/v1/gamecenter/2010020003/play-by-play",
        "https://api-web.nhle.com/v1/gamecenter/2010020003/boxscore",
        "https://api.nhle.com/stats/rest/en/shiftcharts?cayenneExp=gameId=2010020003",
        "https://api-web.nhle.com/v1/roster/PHX/20102011",
        "https://api-web.nhle.com/v1/player/8478402/landing",
    ]
    assert all(store.get(key) == WEEK for key in keys)


def test_cached_copy_is_reused_when_the_rule_accepts_it(tmp_path: Path) -> None:
    requests, handler = serve()
    api = make_api(RawStore(tmp_path), handler)
    first = api.play_by_play(20102011, 2010020003)
    second = api.play_by_play(20102011, 2010020003)
    assert len(requests) == 1
    assert (first.cached, second.cached) == (False, True)
    assert second.raw_key == first.raw_key
    assert second.fetched_utc == first.fetched_utc
    assert (api.requests, api.cache_hits) == (1, 1)


def test_rejected_cache_is_refetched_and_kept(tmp_path: Path) -> None:
    requests, handler = serve()
    store = RawStore(tmp_path)
    api = make_api(store, handler)
    first = api.schedule_week(DAY, never)
    second = api.schedule_week(DAY, never)
    assert len(requests) == 2
    assert second.raw_key != first.raw_key
    assert store.latest("nhl/schedule/2010-10-07") == second.raw_key
    assert store.get(first.raw_key) == WEEK


def test_roster_is_reused_only_when_fetched_after_the_games(tmp_path: Path) -> None:
    requests, handler = serve()
    api = make_api(RawStore(tmp_path), handler)
    fetched = api.roster("PHX", 20102011, never).fetched_utc
    assert api.roster("PHX", 20102011, fetched_after(fetched - timedelta(hours=1))).cached
    assert not api.roster("PHX", 20102011, fetched_after(fetched + timedelta(hours=1))).cached
    assert len(requests) == 2


def test_replay_reads_the_cache_and_never_the_network(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    _, handler = serve()
    cached = make_api(store, handler).schedule_week(DAY, never)
    replay = make_api(store, fail, offline=True)
    # Replay ignores reuse rules: the cache is all there is.
    assert replay.schedule_week(DAY, never).raw_key == cached.raw_key
    with pytest.raises(NotCachedError, match="nhl/player-landing/8478402"):
        replay.player_landing(8478402)
    assert replay.requests == 0


def test_odds_schedule_always_fetches_fresh(tmp_path: Path) -> None:
    requests, handler = serve()
    api = make_api(RawStore(tmp_path), handler)
    games = api.schedule(DAY)
    api.schedule(DAY)
    assert len(requests) == 2
    assert [g.game_id for g in games] == [2010020003, 2010020004, 2010020008]


def test_always_rule() -> None:
    assert always(b"", {}) is True
