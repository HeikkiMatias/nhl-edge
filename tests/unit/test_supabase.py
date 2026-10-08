import json
from datetime import UTC, date, datetime

import httpx
import polars as pl
import pytest

from nhl_edge.lake.supabase import Supabase, SupabaseError

URL = "https://abcdefghijklmnop.supabase.co"
JWT_KEY = "eyJhbGciOiJIUzI1NiJ9.service.role"
SECRET_KEY = "sb_secret_abc123"


def frame(rows: int) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "snapshot_utc": [datetime(2026, 9, 28, 12, 0, 53, tzinfo=UTC)] * rows,
            "line": [None] * rows,
            "price_decimal": [1.91] * rows,
        },
        schema={
            "snapshot_utc": pl.Datetime("us", "UTC"),
            "line": pl.Float64,
            "price_decimal": pl.Float64,
        },
    )


def capture(status: int = 201) -> tuple[list[httpx.Request], httpx.Client]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json={"message": "duplicate"} if status >= 400 else None)

    return requests, httpx.Client(transport=httpx.MockTransport(handler))


def test_insert_batches_and_skips_duplicates() -> None:
    requests, client = capture()
    sent = Supabase(URL, JWT_KEY, client).insert_new("odds_snapshots", frame(1201), ["a", "b"])
    assert sent == 1201
    assert [len(json.loads(r.content)) for r in requests] == [500, 500, 201]
    request = requests[0]
    assert str(request.url) == f"{URL}/rest/v1/odds_snapshots?on_conflict=a%2Cb"
    assert request.headers["Prefer"] == "resolution=ignore-duplicates,return=minimal"
    assert json.loads(request.content)[0] == {
        "snapshot_utc": "2026-09-28T12:00:53Z",
        "line": None,
        "price_decimal": 1.91,
    }


def test_legacy_jwt_key_is_also_a_bearer_token() -> None:
    requests, client = capture()
    Supabase(URL, JWT_KEY, client).insert_new("t", frame(1), ["a"])
    assert requests[0].headers["apikey"] == JWT_KEY
    assert requests[0].headers["Authorization"] == f"Bearer {JWT_KEY}"


def test_secret_key_is_sent_as_apikey_only() -> None:
    requests, client = capture()
    Supabase(URL, SECRET_KEY, client).insert_new("t", frame(1), ["a"])
    assert requests[0].headers["apikey"] == SECRET_KEY
    assert "Authorization" not in requests[0].headers


def test_rejected_write_raises_without_the_key() -> None:
    _, client = capture(status=409)
    with pytest.raises(SupabaseError, match="409") as info:
        Supabase(URL, SECRET_KEY, client).insert_new("t", frame(1), ["a"])
    assert SECRET_KEY not in str(info.value)


def test_project_ref() -> None:
    assert Supabase(URL, SECRET_KEY, httpx.Client()).project_ref == "abcdefghijklmnop"


def test_upsert_replaces_on_conflict_and_serializes_dates() -> None:
    requests, client = capture()
    dated = pl.DataFrame(
        {"game_id": [2010020003], "game_date": [date(2010, 10, 7)]},
        schema={"game_id": pl.Int64, "game_date": pl.Date},
    )
    sent = Supabase(URL, SECRET_KEY, client).upsert("games", dated, ["game_id"])
    assert sent == 1
    request = requests[0]
    assert str(request.url) == f"{URL}/rest/v1/games?on_conflict=game_id"
    assert request.headers["Prefer"] == "resolution=merge-duplicates,return=minimal"
    assert json.loads(request.content) == [{"game_id": 2010020003, "game_date": "2010-10-07"}]


def test_ping_is_one_tiny_read() -> None:
    requests, client = capture(status=200)
    Supabase(URL, SECRET_KEY, client).ping("games", "game_id")
    assert requests[0].method == "GET"
    assert str(requests[0].url) == f"{URL}/rest/v1/games?select=game_id&limit=1"


def test_failed_ping_raises() -> None:
    _, client = capture(status=503)
    with pytest.raises(SupabaseError, match="read from games returned 503"):
        Supabase(URL, SECRET_KEY, client).ping("games", "game_id")


def test_update_patches_the_matching_rows_only() -> None:
    requests, client = capture(204)
    Supabase(URL, SECRET_KEY, client).update(
        "paper_bets",
        {"game_date": date(2026, 10, 8), "game_id": 2026020053},
        {"won": True, "settled_utc": datetime(2026, 10, 9, 9, 7, tzinfo=UTC)},
    )
    (request,) = requests
    assert request.method == "PATCH"
    assert request.url.params["game_date"] == "eq.2026-10-08"
    assert request.url.params["game_id"] == "eq.2026020053"
    assert request.headers["Prefer"] == "return=minimal"
    assert json.loads(request.content) == {"won": True, "settled_utc": "2026-10-09T09:07:00Z"}
