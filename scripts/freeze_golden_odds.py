"""Freeze the golden odds snapshot into tests/golden/odds/ (issue #10, docs/plan.md section 11).

You run this, not Claude: hard rule 10 and the hooks keep Claude from writing to tests/golden/.

    uv run nhl lake restore-raw   # if the snapshot is not in data/raw/ yet
    uv run python scripts/freeze_golden_odds.py

It freezes one stored Odds API snapshot, the pre7 slot of 2026-09-30 at 01:36 UTC, and the NHL
schedule of 2026-09-29, the date of VAN at EDM, which the snapshot quotes 24 minutes before the
start and which went to overtime. The snapshot comes from the local raw cache. The schedule is the
week from 2026-09-27 that the nightly ingest stored at 10:30 UTC on 2026-09-30, with every game of
2026-09-29 final; without it in the cache, the week is fetched once from the NHL API. Each
response is copied byte for byte as stored, gzipped with its .meta.json sidecar, and
tests/golden/odds/manifest.json lists the case and every file's SHA-256. Golden files are frozen:
the script stops rather than overwrite one.
"""

import hashlib
import json
import sys
from datetime import date
from pathlib import Path

from nhl_edge.ingest.games import settled_on
from nhl_edge.ingest.nhl_api import NhlApi, utc_now
from nhl_edge.lake.raw import SUFFIX, RawStore

GOLDEN_DIR = Path("tests/golden")
ODDS_DIR = GOLDEN_DIR / "odds"
MANIFEST = ODDS_DIR / "manifest.json"

SNAPSHOT = "odds/2026-09-30/20260930T013617Z_pre7_eu"
GAME_ID = 2026020004
SEASON = 20262027
GAME_DATE = date(2026, 9, 29)
# The schedule week the nightly ingest read 2026-09-29's results from.
SCHEDULE_WEEK = date(2026, 9, 27)
DESCRIPTION = (
    "VAN at EDM, 6-5 in overtime. The pre7 snapshot at 01:36 UTC, 24 minutes before the NHL's "
    "02:00 start (the Odds API lists 02:10), quotes it on the two-way moneyline (h2h, 8 books) "
    "and the 3-way regulation line (h2h_3_way, 13 books), with 20 games on h2h and 28 on "
    "h2h_3_way in all."
)


def stored(store: RawStore, raw_key: str, name: str) -> dict[str, bytes] | None:
    """One stored response and its sidecar, by their file names in tests/golden/odds/, or None
    unless both are in the raw cache: a body without its sidecar is an interrupted write."""
    sources = {
        f"{name}{suffix}": store.base_dir / f"{raw_key}{suffix}"
        for suffix in (SUFFIX, ".meta.json")
    }
    if not all(path.exists() for path in sources.values()):
        return None
    return {target: path.read_bytes() for target, path in sources.items()}


def freeze(files: dict[str, bytes]) -> dict[str, str]:
    """Write the files to tests/golden/odds/. Returns each file's SHA-256 by its path under
    tests/golden/."""
    hashes = {}
    for name, data in files.items():
        target = ODDS_DIR / name
        with target.open("xb") as f:  # a golden file is never overwritten
            f.write(data)
        hashes[target.relative_to(GOLDEN_DIR).as_posix()] = hashlib.sha256(data).hexdigest()
    return hashes


def main() -> int:
    if MANIFEST.exists():
        print(f"{MANIFEST} exists: the golden odds are frozen already.", file=sys.stderr)
        return 1
    store = RawStore()
    snapshot_name = f"snapshot_{SNAPSHOT.rsplit('/', 1)[1]}"
    schedule_name = f"schedule_{SCHEDULE_WEEK.isoformat()}"
    snapshot = stored(store, SNAPSHOT, snapshot_name)
    if snapshot is None:
        print(
            f"{SNAPSHOT} is not complete in the raw cache: run nhl lake restore-raw",
            file=sys.stderr,
        )
        return 1
    api = NhlApi(store)
    week = api.schedule_week(SCHEDULE_WEEK, settled_on({GAME_DATE}))
    schedule = stored(store, week.raw_key, schedule_name)
    if schedule is None:
        print(f"{week.raw_key} is not complete in the raw cache", file=sys.stderr)
        return 1
    # Every source is read, and no target exists, before the first file is written, so a failed
    # run leaves nothing behind in the protected directory.
    staged = snapshot | schedule
    existing = sorted(name for name in staged if (ODDS_DIR / name).exists())
    if existing:
        print(f"{', '.join(existing)} exist in {ODDS_DIR}: remove them by hand", file=sys.stderr)
        return 1
    ODDS_DIR.mkdir(parents=True, exist_ok=True)
    files = freeze(staged)
    manifest = {
        "frozen_utc": utc_now().isoformat(),
        "snapshot": {"raw_key": SNAPSHOT, "file": f"odds/{snapshot_name}{SUFFIX}"},
        "overtime_game": {
            "game_id": GAME_ID,
            "season": SEASON,
            "game_date": GAME_DATE.isoformat(),
            "schedule": f"odds/{schedule_name}{SUFFIX}",
            "description": DESCRIPTION,
        },
        "files": files,
    }
    with MANIFEST.open("x") as f:
        f.write(json.dumps(manifest, indent=2) + "\n")
    print(f"{MANIFEST} written; {api.requests} NHL requests, {api.cache_hits} cache hits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
