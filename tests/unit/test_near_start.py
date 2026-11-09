"""Near-start snapshots (ADR 0060, #254): one h2h call about 5 minutes before each start time,
within the free tier's credits, stored and replayed apart from odds_snapshots, and reported beside
the closing proxy. On the Odds API and NHL schedule fixtures and synthetic quotes."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
from closing_fixtures import pair, quotes

from nhl_edge.ingest import nhl_api, odds
from nhl_edge.ingest.nhl_api import NhlApi, ScheduledGame
from nhl_edge.ingest.odds import (
    CREDIT_SOURCES,
    NEAR_START_SOURCE,
    OddsApi,
    covered,
    due_starts,
    remaining_credits,
    reserve,
    run_near_start,
    slot_credits_per_day,
)
from nhl_edge.ingest.odds_lake import replay_near_start
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import NEAR_START_SLOT
from nhl_edge.lake.tables import Lake
from nhl_edge.live import report as lr
from nhl_edge.live import revalue
from nhl_edge.live import settle as ls
from nhl_edge.market import closing

FIXTURES = Path(__file__).parent / "fixtures"
ODDS_BODY = (FIXTURES / "odds_api" / "odds_eu_full_20260928T120053Z.json").read_bytes()
WEEK = (FIXTURES / "nhl_api" / "schedule_2026-09-28.json").read_bytes()
# The fixture week's first game, CAR's, starts at 21:00 UTC on 2026-09-29 (17:00 ET).
FIRST = datetime(2026, 9, 29, 21, tzinfo=UTC)
NOW = FIRST - timedelta(minutes=5)
PER_DAY = slot_credits_per_day() + revalue.CALLS_PER_DAY


def nhl_client(store: RawStore, now: datetime = NOW) -> NhlApi:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=WEEK)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=nhl_api.BASE_URL)
    # Each run fetches the schedule at its own time, as each job does.
    clock = iter(now + timedelta(seconds=i) for i in range(100))
    return NhlApi(store, client, min_interval_s=0, now=lambda: next(clock))


def odds_client(calls: list[httpx.Request], remaining: int = 400, at: datetime = NOW) -> OddsApi:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        headers = {"x-requests-remaining": str(remaining), "x-requests-used": "100"}
        return httpx.Response(200, content=ODDS_BODY, headers=headers | {"x-requests-last": "1"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=odds.BASE_URL)
    return OddsApi("secret-key", client, now=lambda: at)


def stored(store: RawStore, source: str, when: datetime, remaining: int | None = None) -> str:
    credits = {"remaining": remaining, "used": 1, "last": 1}
    return store.put(
        source,
        f"{when:%Y-%m-%d}/{when:%Y%m%dT%H%M%SZ}_x_eu",
        ODDS_BODY,
        {"fetched_utc": when.isoformat(), "credits": credits},
    )


def run(
    store: RawStore, calls: list[httpx.Request], now: datetime = NOW, **kw: Any
) -> tuple[str | None, list[str]]:
    lines: list[str] = []
    key = run_near_start(
        nhl=nhl_client(store, now),
        odds=odds_client(calls, at=now, **kw),
        store=store,
        now=now,
        per_day=PER_DAY,
        echo=lines.append,
    )
    return key, lines


# Which starts are due, and the credits kept for the slots


def game(start: datetime, game_type: int = 2) -> ScheduledGame:
    return ScheduledGame(1, game_type, start, "CAR", "FLA")


def test_a_start_is_due_within_fifteen_minutes_of_now_once_per_start_time() -> None:
    games = [game(FIRST), game(FIRST), game(FIRST + timedelta(minutes=30)), game(FIRST, 1)]
    assert due_starts(games, FIRST - timedelta(minutes=5)) == [FIRST]
    assert due_starts(games, FIRST - timedelta(minutes=15)) == [FIRST]
    assert due_starts(games, FIRST - timedelta(minutes=16)) == []
    # A started game is never due; a preseason game alone never is.
    assert due_starts(games, FIRST) == []
    assert due_starts([game(FIRST, 1)], NOW) == []


def test_the_reserve_is_the_rest_of_the_months_slots_and_revaluation_calls() -> None:
    # 10 credits of slots and 4 re-valuation calls a day, today included.
    assert slot_credits_per_day() == 10 and PER_DAY == 14
    assert reserve(datetime(2026, 11, 9, 20, tzinfo=UTC), PER_DAY) == 22 * 14
    assert reserve(datetime(2026, 11, 30, 23, tzinfo=UTC), PER_DAY) == 14
    assert reserve(datetime(2026, 12, 1, tzinfo=UTC), PER_DAY) == 31 * 14
    # Every job's responses count toward the credits left, re-valuation's included.
    assert revalue.SOURCE in CREDIT_SOURCES and NEAR_START_SOURCE in CREDIT_SOURCES


def test_the_credits_left_are_the_newest_stored_responses_this_month(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    assert remaining_credits(store, NOW) is None
    stored(store, "odds", NOW - timedelta(hours=8), remaining=450)
    stored(store, "odds-revalue", NOW - timedelta(hours=1), remaining=440)
    stored(store, NEAR_START_SOURCE, NOW - timedelta(days=1), remaining=460)
    assert remaining_credits(store, NOW) == 440
    # A new month starts afresh: last month's responses don't count.
    assert remaining_credits(store, datetime(2026, 10, 1, 1, tzinfo=UTC)) is None


def test_a_start_is_covered_by_a_snapshot_within_fifteen_minutes_before_it(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    assert not covered(store, FIRST)
    stored(store, NEAR_START_SOURCE, FIRST - timedelta(minutes=20))
    assert not covered(store, FIRST)
    stored(store, NEAR_START_SOURCE, FIRST - timedelta(minutes=5))
    assert covered(store, FIRST)
    # Across UTC midnight: a 00:05 start covered from the day before's 23:55.
    midnight = datetime(2026, 9, 30, 0, 5, tzinfo=UTC)
    stored(store, NEAR_START_SOURCE, midnight - timedelta(minutes=10))
    assert covered(store, midnight)


# The job


def test_with_no_start_due_no_call_is_made(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    key, lines = run(RawStore(tmp_path), calls, now=FIRST - timedelta(hours=2))
    assert key is None and not calls
    assert lines == ["odds near-start: no game starts within 15 minutes, no Odds API call made"]


def test_a_due_start_gets_one_h2h_snapshot_stored_apart_from_the_slots(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    calls: list[httpx.Request] = []
    key, lines = run(store, calls)
    ((request,),) = [calls]
    assert request.url.params["markets"] == "h2h" and request.url.params["regions"] == "eu"
    assert key == f"{NEAR_START_SOURCE}/2026-09-29/20260929T205500Z_{NEAR_START_SLOT}_eu"
    assert key is not None
    meta = store.meta(key)
    assert meta["starts"] == [FIRST.isoformat()] and meta["credits"]["remaining"] == 400
    # Never under odds/, so never in odds_snapshots or the closing proxy; the key never logged.
    assert not (tmp_path / "odds").exists()
    assert "secret-key" not in "\n".join(lines) and "2 events" in lines[0]
    # The next run in the same window finds the start covered and makes no call.
    again: list[httpx.Request] = []
    key, lines = run(store, again, now=NOW + timedelta(minutes=2))
    assert key is None and not again
    assert lines == ["odds near-start: 1 start times due, each has its snapshot already"]


def test_no_call_while_the_credits_left_are_kept_for_the_slots(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    # Two days left in September keep 28 credits: 20 left can't spare one.
    stored(store, "odds", NOW - timedelta(hours=2), remaining=20)
    calls: list[httpx.Request] = []
    key, lines = run(store, calls)
    assert key is None and not calls
    assert lines == [
        "::warning::odds near-start: skipped, 20 credits left this month and the slots and "
        "re-valuation keep 28"
    ]
    stored(store, "odds", NOW - timedelta(hours=1), remaining=29)
    key, _ = run(store, calls, now=NOW + timedelta(minutes=1))
    assert key is not None and len(calls) == 1


def test_the_workflow_runs_the_job_on_the_timers_dispatch() -> None:
    workflow = Path(".github/workflows/odds-near-start.yml").read_text()
    assert "workflow_dispatch:" in workflow
    assert "run: uv run nhl odds near-start --mirror-raw" in workflow
    assert "vars.TIMER_ACTIVE != 'true'" in workflow
    # Only the Odds API key and R2: no Kalshi, Supabase or Telegram secret.
    assert "KALSHI" not in workflow and "SUPABASE" not in workflow


# The lake and the report


def test_the_replay_keeps_the_near_start_snapshots_apart(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    store.put("nhl", "schedule/2026-09-28/20260928T120000Z", WEEK, {"fetched_utc": "x"})
    at = datetime(2026, 9, 28, 12, 0, 53, tzinfo=UTC)
    stored(store, NEAR_START_SOURCE, at)
    lake = Lake(tmp_path / "lake")
    snapshots, rows = replay_near_start(store, lake)
    table = lake.read("odds_near_start")
    assert snapshots == 1 and rows == table.height > 0
    assert set(table["slot"]) == {NEAR_START_SLOT} and not table["is_closing_proxy"].any()
    assert table["game_id"].drop_nulls().len() == table.height
    assert lake.read("odds_snapshots").is_empty()


START = datetime(2026, 10, 7, 23, tzinfo=UTC)


def near_pair(event: str, start: datetime, before: timedelta, age: timedelta, home: float) -> Any:
    rows = pair(start - before, start=start, age=age, event=event)
    for row in rows:
        row["price_decimal"] = home if row["side"] == "home" else 2.1
        row["slot"] = NEAR_START_SLOT
    return rows


def test_the_near_start_pair_is_the_latest_fresh_one_just_before_the_start() -> None:
    minute = timedelta(minutes=1)
    near = quotes(
        near_pair("e1", START, 12 * minute, minute, 1.7),
        near_pair("e1", START, 5 * minute, minute, 1.8),
        # Stale, too early, and at the start itself: never.
        near_pair("e2", START, 5 * minute, 10 * minute, 1.8),
        near_pair("e3", START, 20 * minute, minute, 1.8),
        near_pair("e4", START, 0 * minute, minute, 1.8),
    )
    found = closing.near_start_pairs(near, quotes(), START + timedelta(hours=3))
    assert found.rows() == [("e1", START - 5 * minute, 1.8, 2.1)]
    # A game not started yet has none.
    assert closing.near_start_pairs(near, quotes(), START - minute).is_empty()


def bets(weeks: int = 4) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Two settled home bets at 2.0 a week; the second each week has no pre-game slot."""
    rows, near = [], []
    for i in range(2 * weeks):
        start = START + timedelta(days=7 * (i // 2) + (i % 2))
        status = closing.PROXY if i % 2 == 0 else closing.NO_PREGAME
        rows.append(
            {
                "game_date": start.date(),
                "season": 20262027,
                "game_id": 2026020000 + i,
                "event_id": f"e{i}",
                "side": "home",
                "price": 2.0,
                "home_price": 2.0,
                "away_price": 1.9,
                "settled": ls.SETTLED,
                "close_status": status,
                "clv": 0.05 if status == closing.PROXY else None,
                "fair_move": 0.0,
            }
        )
        near += near_pair(f"e{i}", start, timedelta(minutes=5), timedelta(minutes=1), 1.8)
    return pl.DataFrame(rows), quotes(*[near])


def test_the_report_shows_clv_at_the_near_start_pair_beside_the_proxys() -> None:
    frame, near = bets()
    found = lr.near_start(frame, quotes(), near, START + timedelta(days=30))
    p_home = (1 / 1.8) / (1 / 1.8 + 1 / 2.1)
    assert found["bets"] == 8 and found["first"] == date(2026, 10, 7).isoformat()
    assert found["clv_per_bet"]["mean"] == pytest.approx(2.0 * p_home - 1)
    both = found["with_proxy"]
    assert both["bets"] == 4
    assert both["proxy_clv_per_bet"]["mean"] == pytest.approx(0.05)
    assert both["near_less_proxy"]["mean"] == pytest.approx(2.0 * p_home - 1 - 0.05)
    assert found["no_pregame"]["bets"] == 4
    lines = lr._near_start_lines(found)
    assert lines[0].startswith(
        "- Against Pinnacle about 5 minutes before the start (ADR 0060), from 2026-10-07: CLV "
    )
    # Before any near-start snapshot, and under 4 weeks, counts only.
    assert lr.near_start(frame, quotes(), None, START) == {"first": None, "bets": 0}
    assert lr._near_start_lines({"first": None, "bets": 0}) == [
        "- Against Pinnacle about 5 minutes before the start (ADR 0060): no settled bet has one "
        "yet."
    ]
    few, few_near = bets(weeks=2)
    short = lr.near_start(few, quotes(), few_near, START + timedelta(days=30))
    assert short["clv_per_bet"] == {"games": 4, "weeks": 2}
