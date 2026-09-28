from datetime import UTC, date, datetime
from pathlib import Path

import pandera.errors
import polars as pl
import pytest

from nhl_edge.ingest.players import (
    boxscore_player_ids,
    landing_row,
    parse_players,
    roster_player_ids,
)

FIXTURES = Path(__file__).parent / "fixtures" / "nhl_api"
FETCHED = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)


def landing(player_id: int) -> dict[str, object]:
    body = (FIXTURES / f"landing_{player_id}.json").read_bytes()
    return landing_row(body, FETCHED, f"nhl/player-landing/{player_id}/20260928T130000Z")


def test_roster_ids_cover_every_group() -> None:
    ids = roster_player_ids((FIXTURES / "roster_PHX_20102011.json").read_bytes())
    assert len(ids) == 5  # two forwards, two defensemen, one goalie
    assert roster_player_ids(b"{}") == set()


def test_boxscore_ids_cover_both_teams() -> None:
    ids = boxscore_player_ids((FIXTURES / "boxscore_2010020003.json").read_bytes())
    assert len(ids) == 10
    assert 8473404 in ids  # Backstrom, MIN goalie


def test_drafted_player() -> None:
    frame = parse_players([landing(8478402)])
    assert frame.row(0, named=True) == {
        "player_id": 8478402,
        "name": "Connor McDavid",
        "birth_date": date(1997, 1, 13),
        "position": "C",
        "shoots": "L",
        "draft_year": 2015,
        "draft_overall": 1,
        "fetched_utc": FETCHED,
        "raw_key": "nhl/player-landing/8478402/20260928T130000Z",
    }


def test_undrafted_player_has_no_draft() -> None:
    frame = parse_players([landing(8462535)])
    assert frame.select("name", "draft_year", "draft_overall").row(0) == (
        "Jody Shelley",
        None,
        None,
    )


def test_schema_rejects_half_a_draft_and_unknown_positions() -> None:
    frame = parse_players([landing(8478402)])
    with pytest.raises(pandera.errors.SchemaError, match="drafted_or_not"):
        parse_players([{**landing(8478402), "draft_overall": None}])
    with pytest.raises(pandera.errors.SchemaError):
        parse_players([{**landing(8478402), "position": "F"}])
    with pytest.raises(pandera.errors.SchemaError):
        parse_players([landing(8478402), landing(8478402)])  # duplicate player_id
    assert frame.schema["draft_year"] == pl.Int16
