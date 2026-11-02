"""Point in time for the demo quotes (#375, ADR 0055, hard rule 1): a step reads the fair value only
after its publication, and Daily Faceoff's starters only from a page fetched by then. SYNTHETIC
data only (#280)."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from nhl_edge.ingest import dailyfaceoff as dfo
from nhl_edge.live import bundle as lb
from nhl_edge.live import demo_quotes as dq
from nhl_edge.live import kalshi_quotes as kq
from nhl_edge.live import morning as lm

DAY = date(2026, 11, 4)
START = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
GAME = kq.Game(2026020150, 20262027, DAY, "BOS", "NYR", START)
PUBLISHED = datetime(2026, 11, 4, 12, 15, tzinfo=UTC)


def test_the_fair_value_is_read_only_after_its_publication(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    rows = pl.DataFrame(
        {
            "game_id": [GAME.game_id],
            "status": ["predicted"],
            "p_blend": [0.55],
            "published_utc": [PUBLISHED],
        }
    ).with_columns(pl.col("published_utc").cast(pl.Datetime("us", "UTC")))
    store.put(lm.key(DAY), lb.parquet(rows))
    fair = dq.fair_reader(store, DAY)
    assert fair(GAME, PUBLISHED) is None
    assert fair(GAME, PUBLISHED + timedelta(microseconds=1)) == (0.55, PUBLISHED)


class Raw:
    """A raw store holding one Daily Faceoff page, fetched at fetched."""

    def __init__(self, fetched: datetime) -> None:
        self.fetched = fetched

    def latest_before(self, prefix: str, at: datetime) -> str | None:
        return "dailyfaceoff/page" if at >= self.fetched - timedelta(minutes=1) else None

    def meta(self, key: str) -> dict[str, Any]:
        return {"fetched_utc": self.fetched.isoformat()}

    def get(self, key: str) -> bytes:
        return b""


def test_starters_come_only_from_a_page_fetched_by_then(monkeypatch: pytest.MonkeyPatch) -> None:
    named = pl.DataFrame(
        {"team": ["BOS", "NYR"], "start_utc": [START, START], "dfo_goalie_id": [1, 2]}
    ).with_columns(pl.col("start_utc").cast(pl.Datetime("us", "UTC")))
    monkeypatch.setattr(dfo, "parse_starting_goalies", lambda *_: named)
    fetched = datetime(2026, 11, 4, 15, tzinfo=UTC)
    read = dq.starters_reader(Raw(fetched))  # type: ignore[arg-type]
    # A page whose key is stamped before the moment but fetched after it is not known yet.
    assert read(GAME, fetched - timedelta(seconds=30)) is None
    assert read(GAME, fetched) == {"BOS": 1, "NYR": 2}
