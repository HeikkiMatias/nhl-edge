import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest

from nhl_edge.ingest import nhl_api, odds
from nhl_edge.ingest.nhl_api import NhlApi, ScheduledGame, scheduled_games
from nhl_edge.ingest.odds import (
    ODDS_API_TEAMS,
    SLOT_PLANS,
    OddsApi,
    OddsApiError,
    parse_odds,
    resolve_slot,
    run_snapshot,
    slot_by_name,
    slot_has_games,
    supabase_window,
    team_code,
)
from nhl_edge.lake.raw import RawStore

FIXTURES = Path(__file__).parent / "fixtures"
ODDS_BODY = (FIXTURES / "odds_api" / "odds_eu_full_20260928T120053Z.json").read_bytes()
SCHEDULE_BODY = (FIXTURES / "nhl_api" / "schedule_2026-09-28.json").read_bytes()
SNAPSHOT = datetime(2026, 9, 28, 12, 0, 53, tzinfo=UTC)
WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "odds-snapshots.yml"
EDT_DAY = date(2026, 10, 15)
EST_DAY = date(2026, 12, 15)


def parse(body: bytes = ODDS_BODY, snapshot: datetime = SNAPSHOT) -> pl.DataFrame:
    return parse_odds(body, snapshot, "morning", "odds/test")


def event(markets: list[dict[str, Any]], home: str = "Boston Bruins") -> dict[str, Any]:
    return {
        "id": "evt1",
        "commence_time": "2026-10-01T23:00:00Z",
        "home_team": home,
        "away_team": "Buffalo Sabres",
        "bookmakers": [{"key": "book", "last_update": "2026-09-28T11:00:00Z", "markets": markets}],
    }


def body(*events: dict[str, Any]) -> bytes:
    return json.dumps(list(events)).encode()


H2H = {
    "key": "h2h",
    "outcomes": [{"name": "Boston Bruins", "price": 1.8}, {"name": "Buffalo Sabres", "price": 2.1}],
}


# Parsing


def test_fixture_parses_and_validates() -> None:
    df = parse()
    # Two events: marathonbet 3-way (3), nordicbet h2h (2), pinnacle h2h, spreads, totals (6).
    assert df.height == 2 * (3 + 2 + 6)
    assert set(zip(df["away"], df["home"], strict=True)) == {("FLA", "CAR"), ("MTL", "TOR")}
    assert df["last_update_utc"].null_count() == 0
    assert (df["snapshot_utc"] == SNAPSHOT).all()


def test_three_way_line_is_stored_apart_from_the_moneyline() -> None:
    df = parse()
    moneyline = df.filter(pl.col("market") == "h2h")
    three_way = df.filter(pl.col("market") == "h2h_3_way")
    assert set(moneyline["book"]) == {"nordicbet", "pinnacle"}
    assert set(moneyline["side"]) == {"home", "away"}
    assert set(three_way["book"]) == {"marathonbet"}
    assert set(three_way["side"]) == {"home", "draw", "away"}


def test_sides_and_lines() -> None:
    pinnacle = parse().filter(pl.col("book") == "pinnacle", pl.col("home") == "CAR")
    by_market = {m: pinnacle.filter(pl.col("market") == m) for m in ("h2h", "spreads", "totals")}
    assert by_market["h2h"]["line"].null_count() == 2
    spreads = dict(zip(by_market["spreads"]["side"], by_market["spreads"]["line"], strict=True))
    assert spreads["home"] == -spreads["away"]
    assert set(by_market["totals"]["side"]) == {"over", "under"}
    assert by_market["totals"]["line"].n_unique() == 1


def test_market_last_update_wins_over_the_book_last_update() -> None:
    market = {**H2H, "last_update": "2026-09-28T11:30:00Z"}
    df = parse(body(event([market])))
    assert (df["last_update_utc"] == datetime(2026, 9, 28, 11, 30, tzinfo=UTC)).all()
    df = parse(body(event([H2H])))
    assert (df["last_update_utc"] == datetime(2026, 9, 28, 11, 0, tzinfo=UTC)).all()


def test_exchange_lay_markets_are_skipped() -> None:
    lay = {**H2H, "key": "h2h_lay"}
    assert set(parse(body(event([H2H, lay])))["market"]) == {"h2h"}


def test_draw_outside_the_h2h_market_is_rejected() -> None:
    spreads = {
        "key": "spreads",
        "outcomes": [{"name": "Draw", "price": 3.5, "point": 0}],
    }
    with pytest.raises(ValueError, match="unexpected spreads outcome 'Draw'"):
        parse(body(event([spreads])))


def test_unknown_team_fails_loudly() -> None:
    with pytest.raises(ValueError, match="Quebec Nordiques"):
        parse(body(event([H2H], home="Quebec Nordiques")))


def test_team_names_normalize() -> None:
    assert team_code("Montréal Canadiens") == "MTL"
    assert team_code("Montreal Canadiens") == "MTL"
    assert team_code("St. Louis Blues") == "STL"
    assert team_code("St Louis Blues") == "STL"
    assert team_code("Utah Mammoth") == "UTA"
    assert len(set(ODDS_API_TEAMS.values())) == 32


def test_live_window_keeps_pre_game_quotes_within_36_hours() -> None:
    in_window = parse()
    assert supabase_window(in_window).height == in_window.height
    # Toronto starts 35 h after the snapshot; two hours later it falls outside the window.
    later = in_window.with_columns(
        pl.when(pl.col("home") == "TOR")
        .then(pl.col("commence_time_utc") + timedelta(hours=2))
        .otherwise(pl.col("commence_time_utc"))
        .alias("commence_time_utc")
    )
    assert set(supabase_window(later)["home"]) == {"CAR"}
    in_play = parse(snapshot=datetime(2026, 9, 29, 23, 30, tzinfo=UTC))
    assert supabase_window(in_play).is_empty()


# Slots


def workflow_crons() -> list[str]:
    return re.findall(r'cron: "([^"]+)"', WORKFLOW.read_text())


@pytest.mark.parametrize(
    ("cron", "summer", "winter"),
    [
        ("5 11 * * *", "morning", None),
        ("5 12 * * *", None, "morning"),
        ("45 16 * * *", "midday", None),
        ("45 17 * * *", None, "midday"),
        ("45 22 * * *", "pre7", None),
        ("45 23 * * *", "pre8", "pre7"),
        ("45 0 * * *", None, "pre8"),
        ("45 1 * * *", "pre10", None),
        ("45 2 * * *", None, "pre10"),
    ],
)
def test_resolve_slot_in_both_halves_of_the_dst_year(
    cron: str, summer: str | None, winter: str | None
) -> None:
    for day, expected in ((EDT_DAY, summer), (EST_DAY, winter)):
        slot = resolve_slot("free-tier", cron, day)
        assert (slot.name if slot else None) == expected


def test_workflow_serves_every_slot_once_on_any_day() -> None:
    crons = workflow_crons()
    names = sorted(slot.name for slot in SLOT_PLANS["free-tier"])
    for day in (EDT_DAY, date(2026, 10, 31), date(2026, 11, 2), EST_DAY, date(2027, 3, 15)):
        served = [resolve_slot("free-tier", cron, day) for cron in crons]
        assert sorted(slot.name for slot in served if slot) == names, day


def test_resolve_slot_rejects_non_daily_cron() -> None:
    with pytest.raises(ValueError, match="daily cron"):
        resolve_slot("free-tier", "45 23 * * 1", EDT_DAY)


GAMES = scheduled_games(SCHEDULE_BODY)


@pytest.mark.parametrize(
    ("slot", "now", "expected"),
    [
        ("morning", datetime(2026, 9, 28, 11, 5, tzinfo=UTC), False),  # no games until tomorrow
        ("morning", datetime(2026, 9, 29, 11, 5, tzinfo=UTC), True),
        ("pre7", datetime(2026, 9, 29, 22, 45, tzinfo=UTC), True),  # MTL@TOR at 23:00
        ("pre10", datetime(2026, 9, 30, 1, 45, tzinfo=UTC), True),  # VAN@EDM at 02:00
        ("pre8", datetime(2026, 9, 30, 23, 45, tzinfo=UTC), False),  # 23:30 started, next 02:00
    ],
)
def test_slot_has_games(slot: str, now: datetime, expected: bool) -> None:
    assert slot_has_games(slot_by_name("free-tier", slot), GAMES, now) is expected


def test_preseason_games_do_not_count() -> None:
    now = datetime(2026, 9, 22, 11, 5, tzinfo=UTC)
    preseason = ScheduledGame(2026010001, 1, now + timedelta(hours=8), "BOS", "NYR")
    assert not slot_has_games(slot_by_name("free-tier", "morning"), [preseason], now)


def test_scheduled_games_from_fixture() -> None:
    assert len(GAMES) == 8
    first = GAMES[0]
    assert (first.game_id, first.game_type, first.away, first.home) == (2026020001, 2, "FLA", "CAR")
    assert first.start_utc == datetime(2026, 9, 29, 21, 0, tzinfo=UTC)


# Client and job


def nhl_client(store: RawStore) -> NhlApi:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/schedule/2026-09-28"
        return httpx.Response(200, content=SCHEDULE_BODY)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=nhl_api.BASE_URL)
    return NhlApi(store, client, min_interval_s=0, now=lambda: SNAPSHOT)


def odds_client(handler: Any, api_key: str = "test-key", now: datetime = SNAPSHOT) -> OddsApi:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=odds.BASE_URL)
    return OddsApi(api_key, client, now=lambda: now)


def test_no_games_means_no_odds_api_call(tmp_path: Path) -> None:
    store = RawStore(tmp_path)

    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the Odds API must not be called")

    lines: list[str] = []
    run_snapshot(
        slot=slot_by_name("free-tier", "morning"),
        regions="eu",
        skip_if_no_games=True,
        nhl=nhl_client(store),
        odds=odds_client(fail),
        store=store,
        sink=None,
        now=datetime(2026, 9, 28, 11, 5, tzinfo=UTC),
        echo=lines.append,
    )
    assert lines == ["odds snapshot morning: no NHL games in this slot, no Odds API call made"]
    assert list((tmp_path / "nhl" / "schedule").rglob("*.json.gz"))


def test_snapshot_stores_raw_then_writes_the_live_window(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        headers = {"x-requests-remaining": "497", "x-requests-used": "3", "x-requests-last": "3"}
        return httpx.Response(200, content=ODDS_BODY, headers=headers)

    written: list[pl.DataFrame] = []

    def sink(frame: pl.DataFrame) -> int:
        written.append(frame)
        return frame.height

    lines: list[str] = []
    run_snapshot(
        slot=slot_by_name("free-tier", "morning"),
        regions="eu",
        skip_if_no_games=False,
        nhl=nhl_client(store),
        odds=odds_client(handler, api_key="secret-key"),
        store=store,
        sink=sink,
        now=SNAPSHOT,
        echo=lines.append,
    )
    (request,) = requests
    assert request.url.path == "/v4/sports/icehockey_nhl/odds"
    assert request.url.params["markets"] == "h2h,spreads,totals"
    assert request.url.params["regions"] == "eu"
    assert request.url.params["oddsFormat"] == "decimal"
    assert lines[0] == "odds api credits: remaining=497 used=3 last=3"

    raw_key = "odds/2026-09-28/20260928T120053Z_morning_eu"
    assert store.get(raw_key) == ODDS_BODY
    assert "secret-key" not in json.dumps(store.meta(raw_key))
    (frame,) = written
    assert frame.height == 22
    assert (frame["raw_key"] == raw_key).all()


def test_odds_api_errors_never_show_the_key() -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "API key is not valid"})

    with pytest.raises(OddsApiError) as info:
        odds_client(unauthorized, api_key="secret-key").odds("eu", ["h2h"])
    assert "401" in str(info.value)
    assert "secret-key" not in str(info.value)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with pytest.raises(OddsApiError) as info:
        odds_client(unreachable, api_key="secret-key").odds("eu", ["h2h"])
    assert "secret-key" not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


def test_odds_api_rejects_markets_outside_the_plan() -> None:
    with pytest.raises(ValueError, match="h2h_3_way"):
        odds_client(lambda request: httpx.Response(200)).odds("eu", ["h2h_3_way"])
