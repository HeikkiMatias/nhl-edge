"""Freeze the five golden games into tests/golden/ (issue #6, docs/plan.md sections 7 and 11).

You run this, not Claude: hard rule 10 and the hooks keep Claude from writing to tests/golden/.

    uv run python scripts/freeze_golden_games.py

For each game it gets the schedule of the game date, the play-by-play, the boxscore and the shift
chart through the NHL API client. It reuses the local raw cache and fetches only what is missing,
at the client's usual 1 request per second. Each response is copied byte for byte as stored,
gzipped with its .meta.json sidecar, to tests/golden/nhl/. tests/golden/manifest.json lists the
cases and every file's SHA-256. Golden files are frozen: the script stops rather than overwrite one.
"""

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from nhl_edge.ingest.games import settled_on
from nhl_edge.ingest.nhl_api import NhlApi, Response, utc_now
from nhl_edge.lake.raw import SUFFIX, RawStore

GOLDEN_DIR = Path("tests/golden")
FEEDS_DIR = GOLDEN_DIR / "nhl"
MANIFEST = GOLDEN_DIR / "manifest.json"


@dataclass(frozen=True)
class Case:
    name: str
    game_id: int
    season: int
    game_date: date
    description: str


CASES = (
    Case(
        "regulation",
        2023020003,
        20232024,
        date(2023, 10, 10),
        "SEA at VGK, 1-4 in regulation, with a 5-on-6 empty-net goal at 58:45",
    ),
    Case(
        "overtime",
        2023020040,
        20232024,
        date(2023, 10, 17),
        "TBL at BUF, 2-3 in overtime: TBL tie it 6-on-5 at 59:53, BUF win 3-on-3 at 61:46",
    ),
    Case(
        "shootout",
        2023020032,
        20232024,
        date(2023, 10, 14),
        "CAR at LAK, 6-5 in a shootout: LAK tie it 6-on-5 at 58:38, 5-5 after overtime",
    ),
    Case(
        "late_empty_net",
        2023020052,
        20232024,
        date(2023, 10, 19),
        "TOR at FLA, 1-3: FLA score into the empty net at 59:59 while shorthanded, 4 on 6",
    ),
    Case(
        "five_on_three",
        2023020019,
        20232024,
        date(2023, 10, 14),
        "PHI at OTT, 2-5: PHI score 5-on-3 at 16:51 of the first period",
    ),
)


def freeze(store: RawStore, response: Response, name: str) -> dict[str, str]:
    """Copy one stored response and its sidecar to tests/golden/nhl/<name>. Returns each file's
    SHA-256 by its path under tests/golden/."""
    hashes = {}
    for suffix in (SUFFIX, ".meta.json"):
        data = (store.base_dir / f"{response.raw_key}{suffix}").read_bytes()
        target = FEEDS_DIR / f"{name}{suffix}"
        with target.open("xb") as f:  # a golden file is never overwritten
            f.write(data)
        hashes[target.relative_to(GOLDEN_DIR).as_posix()] = hashlib.sha256(data).hexdigest()
    return hashes


def main() -> int:
    if MANIFEST.exists():
        print(f"{MANIFEST} exists: the golden games are frozen already.", file=sys.stderr)
        return 1
    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    store = RawStore()
    api = NhlApi(store)
    frozen_dates: set[date] = set()
    cases = []
    for case in CASES:
        files: dict[str, str] = {}
        if case.game_date not in frozen_dates:
            week = api.schedule_week(case.game_date, settled_on({case.game_date}))
            files |= freeze(store, week, f"schedule_{case.game_date.isoformat()}")
            frozen_dates.add(case.game_date)
        for kind, fetch in (
            ("play-by-play", api.play_by_play),
            ("boxscore", api.boxscore),
            ("shiftcharts", api.shift_chart),
        ):
            files |= freeze(store, fetch(case.season, case.game_id), f"{kind}_{case.game_id}")
        cases.append(
            {
                "case": case.name,
                "game_id": case.game_id,
                "season": case.season,
                "game_date": case.game_date.isoformat(),
                "schedule": f"nhl/schedule_{case.game_date.isoformat()}{SUFFIX}",
                "description": case.description,
                "files": files,
            }
        )
        print(f"{case.name}: {case.game_id} frozen, {len(files)} files")
    manifest = {"frozen_utc": utc_now().isoformat(), "cases": cases}
    with MANIFEST.open("x") as f:
        f.write(json.dumps(manifest, indent=2) + "\n")
    print(f"{MANIFEST} written; {api.requests} NHL requests, {api.cache_hits} cache hits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
