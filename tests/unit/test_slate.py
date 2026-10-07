import json
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest
from pandera.errors import SchemaError

from nhl_edge.lake.schemas import Slate
from nhl_edge.live import slate

FIXTURE = Path(__file__).parent / "fixtures" / "nhl_api" / "schedule_2026-09-28.json"
META = {"fetched_utc": "2026-09-28T23:51:12+00:00", "status": 200}


def body(edit: dict[int, dict[str, object]] | None = None) -> bytes:
    """The fixture's response, with the given games' fields changed."""
    data = json.loads(FIXTURE.read_text())
    for day in data["gameWeek"]:
        for game in day["games"]:
            game.update((edit or {}).get(game["id"], {}))
    return json.dumps(data).encode()


def test_the_slate_is_the_days_scheduled_games_as_known_at_the_fetch() -> None:
    rows = slate.slate_rows(body(), META, date(2026, 9, 30), "nhl/schedule/2026-09-28/x")
    assert rows["game_id"].to_list() == [2026020006, 2026020007, 2026020008]
    assert set(rows["game_date"]) == {date(2026, 9, 30)}
    # observed_utc is the actual fetch time, never a backdated convention.
    assert set(rows["observed_utc"]) == {datetime(2026, 9, 28, 23, 51, 12, tzinfo=UTC)}
    assert set(rows["game_state"]) == {"FUT"}
    assert rows.columns[-2:] == ["observed_utc", "raw_key"]
    # The builders read only a game's schedule columns, never a result.
    assert slate.as_games(rows).columns == [
        "game_id",
        "season",
        "game_date",
        "start_utc",
        "home",
        "away",
    ]


def test_postponed_cancelled_and_preseason_games_have_no_row() -> None:
    edited = body(
        {
            2026020006: {"gameScheduleState": "PPD"},
            2026020007: {"gameScheduleState": "CNCL"},
            2026020008: {"gameType": 1},
        }
    )
    rows = slate.slate_rows(edited, META, date(2026, 9, 30), "k")
    assert rows.is_empty()
    assert rows.columns == list(Slate.to_schema().columns)


def test_a_game_listed_against_itself_is_refused() -> None:
    edited = json.loads(body())
    game = edited["gameWeek"][2]["games"][0]
    game["awayTeam"]["abbrev"] = game["homeTeam"]["abbrev"]
    with pytest.raises(SchemaError):
        slate.slate_rows(json.dumps(edited).encode(), META, date(2026, 9, 30), "k")


def test_the_lake_knows_the_slate_table() -> None:
    from nhl_edge.lake.tables import TABLES

    assert TABLES["slate"].key == ("game_id",)
    assert TABLES["slate"].partition_by == ("season", "game_date")
    assert pl.DataFrame(schema=TABLES["slate"].empty().schema).is_empty()
