import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import polars as pl
import pytest
from pandera.errors import SchemaError

from nhl_edge.ingest import nhl_api
from nhl_edge.ingest.nhl_api import NhlApi
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import Slate
from nhl_edge.live import slate

FIXTURE = Path(__file__).parent / "fixtures" / "nhl_api" / "schedule_2026-09-28.json"
FETCHED = datetime(2026, 9, 28, 23, 51, 12, tzinfo=UTC)


def body(edit: dict[int, dict[str, object]] | None = None) -> bytes:
    """The fixture's response, with the given games' fields changed."""
    data = json.loads(FIXTURE.read_text())
    for day in data["gameWeek"]:
        for game in day["games"]:
            game.update((edit or {}).get(game["id"], {}))
    return json.dumps(data).encode()


def test_the_slate_is_the_days_scheduled_games_as_known_at_the_fetch() -> None:
    rows = slate.slate_rows(body(), FETCHED, date(2026, 9, 30), "nhl/schedule/2026-09-28/x")
    assert rows["game_id"].to_list() == [2026020006, 2026020007, 2026020008]
    assert set(rows["game_date"]) == {date(2026, 9, 30)}
    # observed_utc is the actual fetch time, never a backdated convention.
    assert set(rows["observed_utc"]) == {FETCHED}
    assert set(rows["game_state"]) == {"FUT"}
    assert rows.columns[-2:] == ["observed_utc", "raw_key"]
    assert slate.of_day(rows, date(2026, 9, 30)).height == 3
    assert slate.of_day(rows, date(2026, 9, 29)).is_empty()


def test_postponed_cancelled_and_preseason_games_have_no_row() -> None:
    edited = body(
        {
            2026020006: {"gameScheduleState": "PPD"},
            2026020007: {"gameScheduleState": "CNCL"},
            2026020008: {"gameType": 1},
        }
    )
    rows = slate.slate_rows(edited, FETCHED, date(2026, 9, 30), "k")
    assert rows.is_empty()
    assert rows.columns == list(Slate.to_schema().columns)


def test_a_game_listed_against_itself_is_refused() -> None:
    edited = json.loads(body())
    game = edited["gameWeek"][2]["games"][0]
    game["awayTeam"]["abbrev"] = game["homeTeam"]["abbrev"]
    with pytest.raises(SchemaError):
        slate.slate_rows(json.dumps(edited).encode(), FETCHED, date(2026, 9, 30), "k")


def test_the_lake_knows_the_slate_table() -> None:
    from nhl_edge.lake.tables import TABLES

    assert TABLES["slate"].key == ("game_id",)
    assert TABLES["slate"].partition_by == ("season", "game_date")
    assert pl.DataFrame(schema=TABLES["slate"].empty().schema).is_empty()


def api(store: RawStore, responses: list[bytes], now: datetime) -> tuple[NhlApi, list[str]]:
    """An NhlApi answering schedule requests with the given bodies in turn, and the paths asked."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(200, content=responses[min(len(asked), len(responses)) - 1])

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=nhl_api.BASE_URL)
    return NhlApi(store, client, min_interval_s=0, now=lambda: now), asked


def test_the_slate_is_fetched_fresh_and_cached_raw(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    first, asked = api(store, [body()], FETCHED)
    fetched = slate.fetch(first, date(2026, 9, 30))
    rows = fetched.games
    assert rows["game_id"].to_list() == [2026020006, 2026020007, 2026020008]
    assert set(rows["observed_utc"]) == {fetched.fetched_utc} == {FETCHED}
    assert set(rows["raw_key"]) == {fetched.raw_key}
    assert fetched.raw_key.startswith("nhl/schedule/2026-09-30/")
    assert store.get(fetched.raw_key) == body()
    # A game can be postponed up to its start, so a cached response is never reused.
    later = datetime(2026, 9, 30, 9, tzinfo=UTC)
    second, asked_again = api(store, [body({2026020006: {"gameScheduleState": "PPD"}})], later)
    again = slate.fetch(second, date(2026, 9, 30)).games
    assert asked == asked_again == ["/v1/schedule/2026-09-30"]
    assert again["game_id"].to_list() == [2026020007, 2026020008]
    assert set(again["observed_utc"]) == {later}


def games_of(ids: list[int]) -> pl.DataFrame:
    return pl.DataFrame({"game_id": ids}, schema={"game_id": pl.Int64})


FINAL_29TH: dict[int, dict[str, object]] = {
    game: {"gameState": "OFF"} for game in range(2026020001, 2026020006)
}
DAYS = [date(2026, 9, 28), date(2026, 9, 29)]


def test_the_days_before_a_slate_must_be_final_and_in_the_lake() -> None:
    final = body(FINAL_29TH)
    assert slate.unsettled(final, DAYS, games_of(list(FINAL_29TH))) == []
    # A game still being played, or final and missing from games, would leave its teams'
    # histories short.
    live = body({**FINAL_29TH, 2026020002: {"gameState": "LIVE"}})
    missing = games_of([g for g in FINAL_29TH if g != 2026020004])
    assert slate.unsettled(live, DAYS, missing) == [
        "2026-09-29: game 2026020002 is LIVE, not final",
        "2026-09-29: game 2026020004 is final but not in the lake's games",
    ]
    # A postponed game, and the days after them, don't count.
    postponed = body({**FINAL_29TH, 2026020003: {"gameState": "FUT", "gameScheduleState": "PPD"}})
    held = games_of([g for g in FINAL_29TH if g != 2026020003])
    assert slate.unsettled(postponed, DAYS, held) == []


def test_the_week_before_a_slate_is_read_from_its_schedule(tmp_path: Path) -> None:
    now = datetime(2026, 10, 5, 9, tzinfo=UTC)
    reader, asked = api(RawStore(tmp_path), [body(FINAL_29TH)], now)
    problems = slate.settled_problems(reader, date(2026, 10, 5), games_of([2026020001]))
    assert asked == ["/v1/schedule/2026-09-28"]
    # The four other games of 09-29 are missing, and 09-30's three are not final.
    assert sum("final but not in the lake's games" in p for p in problems) == 4
    assert sum("is FUT, not final" in p for p in problems) == 3
    assert len(problems) == 7


def test_a_day_without_games_still_names_its_response(tmp_path: Path) -> None:
    reader, _ = api(RawStore(tmp_path), [body()], FETCHED)
    fetched = slate.fetch(reader, date(2026, 9, 28))
    assert fetched.games.is_empty()
    assert fetched.raw_key.startswith("nhl/schedule/2026-09-28/")
    assert fetched.fetched_utc == FETCHED


def test_an_opening_night_is_one_with_no_game_of_its_season_final() -> None:
    rows = slate.slate_rows(body(), FETCHED, date(2026, 9, 30), "k")
    earlier = pl.DataFrame({"season": [20252026]}, schema={"season": pl.Int32})
    assert slate.opening(rows, earlier) == [20262027]
    played = pl.DataFrame({"season": [20252026, 20262027]}, schema={"season": pl.Int32})
    assert slate.opening(rows, played) == []
