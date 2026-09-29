"""Nightly contract test (docs/plan.md section 11): one golden game read live from the NHL API must
parse into the same tables as its frozen copy in tests/golden/. A difference means the API changed
under the parsers, and the failing nightly run is the alert.

It calls the live API only with NHL_CONTRACT=1, which .github/workflows/ingest-nightly.yml sets.
Local and CI runs skip it.
"""

import os
from datetime import date
from pathlib import Path

import pytest
from golden_games import CASES, drift

from nhl_edge.ingest.nhl_api import NhlApi, never
from nhl_edge.lake.raw import RawStore

pytestmark = pytest.mark.skipif(
    os.environ.get("NHL_CONTRACT") != "1", reason="calls the live NHL API; set NHL_CONTRACT=1"
)

CASE = CASES["late_empty_net"]


def live_bodies(api: NhlApi) -> dict[str, bytes]:
    """The golden game's four responses, fetched fresh."""
    game_id, season = CASE["game_id"], CASE["season"]
    day = date.fromisoformat(CASE["game_date"])
    return {
        f"schedule_{day.isoformat()}": api.schedule_week(day, never).body,
        f"play-by-play_{game_id}": api.play_by_play(season, game_id).body,
        f"boxscore_{game_id}": api.boxscore(season, game_id).body,
        f"shiftcharts_{game_id}": api.shift_chart(season, game_id).body,
    }


def test_a_golden_game_parses_the_same_live(tmp_path: Path) -> None:
    api = NhlApi(RawStore(tmp_path))  # an empty cache, so every response is fetched live
    bodies = live_bodies(api)
    assert api.cache_hits == 0  # all four live; retries of a transient error may add requests
    problems = drift(CASE, bodies.__getitem__)
    assert not problems, f"NHL API drift on golden game {CASE['game_id']}:\n" + "\n".join(problems)
