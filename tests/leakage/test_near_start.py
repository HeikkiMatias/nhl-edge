"""Near-start snapshots (ADR 0060, #254) stay beside the closing proxy: the replay never puts one in
odds_snapshots, so no proxy, decision or CLV of the policy reads one within the season; only the
report and the storage code name them; and no pair is taken at or after its start (hard rules 1
and 2). On synthetic quotes and the Odds API fixture."""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nhl_edge.ingest.odds import NEAR_START_SOURCE
from nhl_edge.ingest.odds_lake import replay_near_start, replay_odds
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import NEAR_START_SLOT
from nhl_edge.lake.tables import Lake
from nhl_edge.market import closing

FIXTURES = Path(__file__).parents[1] / "unit" / "fixtures"
ODDS = (FIXTURES / "odds_api" / "odds_eu_full_20260928T120053Z.json").read_bytes()
WEEK = (FIXTURES / "nhl_api" / "schedule_2026-09-28.json").read_bytes()
SRC = Path(__file__).parents[2] / "src" / "nhl_edge"
# The only modules that may name the near-start snapshots: where they are taken, stored,
# replayed, paired and reported. Never the decision, its features, the models or settlement.
ALLOWED = {
    "cli.py",
    "ingest/odds.py",
    "ingest/odds_lake.py",
    "lake/schemas.py",
    "lake/tables.py",
    "market/closing.py",
    "live/report.py",
}


def put(store: RawStore, source: str, when: datetime, slot: str) -> None:
    store.put(
        source,
        f"{when:%Y-%m-%d}/{when:%Y%m%dT%H%M%SZ}_{slot}_eu",
        ODDS,
        {"fetched_utc": when.isoformat(), "slot": slot},
    )


def test_a_near_start_snapshot_never_enters_odds_snapshots(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    store.put("nhl", "schedule/2026-09-28/20260928T120000Z", WEEK, {"fetched_utc": "x"})
    slot = datetime(2026, 9, 28, 12, 0, 53, tzinfo=UTC)
    put(store, "odds", slot, "morning")
    # A near-start snapshot later than every slot's, the one a proxy would pick if it saw it.
    put(store, NEAR_START_SOURCE, slot + timedelta(hours=1), NEAR_START_SLOT)
    lake = Lake(tmp_path / "lake")
    replay_odds(store, lake)
    replay_near_start(store, lake)
    snapshots = lake.read("odds_snapshots")
    assert set(snapshots["snapshot_utc"]) == {slot}
    assert NEAR_START_SLOT not in set(snapshots["slot"])
    assert not snapshots["raw_key"].str.starts_with(NEAR_START_SOURCE).any()
    near = lake.read("odds_near_start")
    assert set(near["slot"]) == {NEAR_START_SLOT} and not near["is_closing_proxy"].any()


def test_only_the_report_and_the_storage_code_name_the_near_start_snapshots() -> None:
    pattern = re.compile(r"odds_near_start|NEAR_START|near_start|odds-near-start")
    naming = {
        str(path.relative_to(SRC)) for path in SRC.rglob("*.py") if pattern.search(path.read_text())
    }
    assert naming <= ALLOWED, sorted(naming - ALLOWED)


def test_no_near_start_pair_is_taken_at_or_after_its_start() -> None:
    from closing_fixtures import START, pair, quotes

    near = quotes(pair(START), pair(START + timedelta(minutes=1)))
    assert closing.near_start_pairs(near, quotes(), START + timedelta(hours=3)).is_empty()
    just_before = quotes(pair(START - timedelta(seconds=1)))
    found = closing.near_start_pairs(just_before, quotes(), START + timedelta(hours=3))
    assert found["near_utc"].to_list() == [START - timedelta(seconds=1)]
