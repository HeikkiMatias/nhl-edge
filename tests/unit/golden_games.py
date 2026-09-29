"""The five golden games frozen in tests/golden/ (issue #6), parsed as nhl ingest parses them.

tests/golden/ holds data only, written by scripts/freeze_golden_games.py and committed by hand
(hard rule 10). Its manifest names the cases: regulation, overtime, shootout, late_empty_net and
five_on_three. The golden tests read the frozen responses, and the nightly contract test reads the
same responses live from the API and expects identical tables.
"""

import gzip
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from nhl_edge.ingest.feeds import FeedGame
from nhl_edge.ingest.games import listed_games, parse_games
from nhl_edge.ingest.lineups import parse_actual_lineups
from nhl_edge.ingest.shift_coverage import shift_coverage
from nhl_edge.ingest.shifts import parse_shifts
from nhl_edge.ingest.shots import parse_shots

GOLDEN_DIR = Path(__file__).parents[1] / "golden"
MANIFEST: dict[str, Any] = json.loads((GOLDEN_DIR / "manifest.json").read_text())
CASES: dict[str, dict[str, Any]] = {case["case"]: case for case in MANIFEST["cases"]}
# Every table built from a golden game carries this raw key, so frozen and live parses compare.
RAW_KEY = "golden"

# A response by its golden name: schedule_<date>, play-by-play_<id>, boxscore_<id>,
# shiftcharts_<id>.
Bodies = Callable[[str], bytes]


def frozen_body(name: str) -> bytes:
    return gzip.decompress((GOLDEN_DIR / "nhl" / f"{name}.json.gz").read_bytes())


def response_names(case: dict[str, Any]) -> list[str]:
    game_id = case["game_id"]
    return [
        f"schedule_{case['game_date']}",
        f"play-by-play_{game_id}",
        f"boxscore_{game_id}",
        f"shiftcharts_{game_id}",
    ]


def parse_case(case: dict[str, Any], bodies: Bodies = frozen_body) -> dict[str, pl.DataFrame]:
    """The golden game's row of games and its four per-game tables."""
    game_id, game_date = case["game_id"], date.fromisoformat(case["game_date"])
    schedule, pbp, box, chart = (bodies(name) for name in response_names(case))
    games = parse_games(listed_games(schedule, {game_date}), RAW_KEY).filter(
        pl.col("game_id") == game_id
    )
    game = FeedGame.from_boxscore(games.row(0, named=True), box)
    shots = parse_shots(pbp, game, RAW_KEY)
    shifts, drops = parse_shifts(chart, game, RAW_KEY)
    lineups = parse_actual_lineups(box, game, RAW_KEY)
    return {
        "games": games,
        "shots": shots,
        "shifts": shifts,
        "actual_lineups": lineups,
        "shift_coverage": shift_coverage(game, shots, shifts, lineups, drops, RAW_KEY),
    }


def key_paths(value: Any, prefix: str = "") -> set[str]:
    """Every key path in a JSON value, list items collapsed: plays[].details.xCoord."""
    if isinstance(value, dict):
        paths: set[str] = set()
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            paths |= {path} | key_paths(item, path)
        return paths
    if isinstance(value, list):
        return set().union(*(key_paths(item, f"{prefix}[]") for item in value))
    return set()


def key_changes(case: dict[str, Any], live: Bodies, frozen: Bodies) -> list[str]:
    """The JSON keys that disappeared from or appeared in each live response."""
    changes = []
    for name in response_names(case):
        before = key_paths(json.loads(frozen(name)))
        after = key_paths(json.loads(live(name)))
        if gone := sorted(before - after):
            changes.append(f"{name}: keys gone: {', '.join(gone[:20])}")
        if new := sorted(after - before):
            changes.append(f"{name}: keys new: {', '.join(new[:20])}")
    return changes


def drift(case: dict[str, Any], live: Bodies, frozen: Bodies = frozen_body) -> list[str]:
    """How the tables parsed from a golden game's live responses differ from those parsed from the
    frozen ones, followed by the JSON key changes that may explain it. Empty when the tables are
    identical: a key the parsers never read may come and go without failing anything."""
    try:
        live_tables = parse_case(case, live)
    except Exception as exc:  # any parse failure is drift
        problems = [f"parsing the live responses failed: {exc!r}"]
    else:
        problems = [
            f"{table}: parsed rows differ from the frozen copy"
            for table, expected in parse_case(case, frozen).items()
            if not live_tables[table].equals(expected)
        ]
    return [*problems, *key_changes(case, live, frozen)] if problems else []
