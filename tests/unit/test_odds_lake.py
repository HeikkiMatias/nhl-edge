import gzip
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from nhl_edge.ingest.odds_lake import replay_odds
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.tables import Lake

FIXTURES = Path(__file__).parent / "fixtures"
ODDS = (FIXTURES / "odds_api" / "odds_eu_full_20260928T120053Z.json").read_bytes()
WEEK = (FIXTURES / "nhl_api" / "schedule_2026-09-28.json").read_bytes()
SNAPSHOT = datetime(2026, 9, 28, 12, 0, 53, tzinfo=UTC)
# The fixture's two events and the games the NHL lists for them.
FLA_AT_CAR, MTL_AT_TOR = 2026020001, 2026020002


def store_snapshot(store: RawStore, body: bytes = ODDS, when: datetime = SNAPSHOT) -> str:
    return store.put(
        "odds",
        f"{when:%Y-%m-%d}/{when:%Y%m%dT%H%M%SZ}_morning_eu",
        body,
        {"fetched_utc": when.isoformat(), "slot": "morning"},
    )


def store_schedule(store: RawStore, body: bytes = WEEK, day: str = "2026-09-28") -> None:
    store.put("nhl", f"schedule/{day}/20260928T120000Z", body, {"fetched_utc": "x"})


def edited_week(game_id: int, **changes: Any) -> bytes:
    """The schedule fixture with one game's fields changed."""
    data = json.loads(WEEK)
    for day in data["gameWeek"]:
        for game in day["games"]:
            if game["id"] == game_id:
                game.update(changes)
    return json.dumps(data).encode()


def replayed(tmp_path: Path, store: RawStore) -> pl.DataFrame:
    lake = Lake(tmp_path / "lake")
    replay_odds(store, lake)
    return lake.read("odds_snapshots")


def games_of(table: pl.DataFrame) -> dict[tuple[str, str], int | None]:
    pairs = table.select("away", "home", "game_id").unique().iter_rows()
    return {(away, home): game_id for away, home, game_id in pairs}


def test_events_match_their_listed_games(tmp_path: Path) -> None:
    # The Odds API starts FLA at CAR 47 s and MTL at TOR 10 minutes off the NHL's listed times.
    store = RawStore(tmp_path / "raw")
    store_snapshot(store)
    store_schedule(store)
    report = replay_odds(store, Lake(tmp_path / "lake"))
    assert (report.snapshots, report.events, report.unmatched) == (1, 2, [])
    assert dict(report.matched) == {"regular season": 2}
    table = Lake(tmp_path / "lake").read("odds_snapshots")
    assert games_of(table) == {("FLA", "CAR"): FLA_AT_CAR, ("MTL", "TOR"): MTL_AT_TOR}
    assert set(table["game_type"]) == {2}


def test_an_event_no_listing_matches_keeps_a_null_game(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    store_snapshot(store)
    store_schedule(store, edited_week(MTL_AT_TOR, startTimeUTC="2026-10-01T05:00:00Z"))  # 30 h off
    report = replay_odds(store, Lake(tmp_path / "lake"))
    assert [(home, away) for _, home, away, _ in report.unmatched] == [("TOR", "MTL")]
    table = Lake(tmp_path / "lake").read("odds_snapshots")
    assert games_of(table) == {("FLA", "CAR"): FLA_AT_CAR, ("MTL", "TOR"): None}
    assert table.filter(pl.col("game_id").is_null())["game_type"].is_null().all()


def test_the_listing_nearest_the_event_wins(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    store_snapshot(store)
    # A later schedule lists the TOR game postponed by two weeks, and a rematch three days on.
    store_schedule(store)
    later = edited_week(MTL_AT_TOR, startTimeUTC="2026-10-13T23:00:00Z")
    store.put("nhl", "schedule/2026-09-29/20260929T120000Z", later, {"fetched_utc": "x"})
    rematch = edited_week(MTL_AT_TOR, id=2026020099, startTimeUTC="2026-10-02T23:00:00Z")
    store.put("nhl", "schedule/2026-09-30/20260930T120000Z", rematch, {"fetched_utc": "x"})
    table = replayed(tmp_path, store)
    assert games_of(table)[("MTL", "TOR")] == MTL_AT_TOR


def test_three_way_quotes_stay_out_of_the_moneyline(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    store_snapshot(store)
    store_schedule(store)
    table = replayed(tmp_path, store)
    assert set(table.filter(pl.col("market") == "h2h")["side"]) == {"home", "away"}
    assert "draw" in set(table.filter(pl.col("market") == "h2h_3_way")["side"])


def test_partitions_follow_the_snapshot_date_and_replays_agree(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    store_snapshot(store)
    store_snapshot(store, when=SNAPSHOT + timedelta(days=1))
    store_schedule(store)
    lake = Lake(tmp_path / "lake")
    replay_odds(store, lake)
    first = lake.read("odds_snapshots")
    partitions = sorted(
        p.parent.name for p in (tmp_path / "lake/odds_snapshots").rglob("*.parquet")
    )
    assert partitions == ["snapshot_date=2026-09-28", "snapshot_date=2026-09-29"]
    replay_odds(store, lake)
    assert lake.read("odds_snapshots").equals(first)
    # A window replays only its own dates.
    report = replay_odds(store, lake, [date(2026, 9, 29)])
    assert report.dates == [date(2026, 9, 29)]
    assert report.snapshots == 1


def test_a_snapshot_without_its_sidecar_is_skipped_and_reported(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    store_schedule(store)
    path = tmp_path / "raw/odds/2026-09-28/20260928T120053Z_morning_eu.json.gz"
    path.parent.mkdir(parents=True)
    path.write_bytes(gzip.compress(ODDS))
    report = replay_odds(store, Lake(tmp_path / "lake"))
    assert report.incomplete == ["odds/2026-09-28/20260928T120053Z_morning_eu"]
    assert report.snapshots == 0
    assert Lake(tmp_path / "lake").read("odds_snapshots").is_empty()


@pytest.mark.parametrize("missing", ["schedule", "snapshot"])
def test_replay_needs_no_network(tmp_path: Path, missing: str) -> None:
    # Nothing to call: with no schedule every event is unmatched, with no snapshot nothing happens.
    store = RawStore(tmp_path / "raw")
    if missing != "snapshot":
        store_snapshot(store)
    if missing != "schedule":
        store_schedule(store)
    report = replay_odds(store, Lake(tmp_path / "lake"))
    assert len(report.unmatched) == (2 if missing == "schedule" else 0)


def test_a_date_without_quotes_left_loses_its_partition(tmp_path: Path) -> None:
    # A parser fix or a stricter filter can leave a replayed date with no quotes: its old
    # partition must go too, not linger with rows the replay no longer makes.
    store = RawStore(tmp_path / "raw")
    raw_key = store_snapshot(store)
    store_schedule(store)
    lake = Lake(tmp_path / "lake")
    replay_odds(store, lake)
    assert lake.read("odds_snapshots").height > 0
    for suffix in (".json.gz", ".meta.json"):
        (tmp_path / "raw" / f"{raw_key}{suffix}").unlink()
    report = replay_odds(store, lake, [date(2026, 9, 28)])
    assert report.snapshots == 0
    assert lake.read("odds_snapshots").is_empty()


def test_other_nhl_game_types_are_kept_and_named(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    store_snapshot(store)
    store_schedule(store, edited_week(MTL_AT_TOR, gameType=4))  # as if an All-Star game
    report = replay_odds(store, Lake(tmp_path / "lake"))
    assert dict(report.matched) == {"regular season": 1, "game type 4": 1}
    table = Lake(tmp_path / "lake").read("odds_snapshots")
    assert set(table["game_type"]) == {2, 4}


def priced(commence: datetime, snapshot: datetime) -> bytes:
    """The fixture's first event at Pinnacle alone, starting at commence, its prices updated a
    minute before snapshot."""
    event = json.loads(ODDS)[0]
    updated = (snapshot - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    event["commence_time"] = commence.strftime("%Y-%m-%dT%H:%M:%SZ")
    event["bookmakers"] = [b for b in event["bookmakers"] if b["key"] == "pinnacle"]
    for book in event["bookmakers"]:
        book["last_update"] = updated
        for market in book["markets"]:
            market["last_update"] = updated
    return json.dumps([event]).encode()


def flagged_at(lake: Lake) -> set[datetime]:
    return set(lake.read("odds_snapshots").filter("is_closing_proxy")["snapshot_utc"])


def test_the_closing_proxy_is_derived_over_the_whole_history(tmp_path: Path) -> None:
    # A 20:00 EST game (01:00 UTC next day): its pre7 snapshot is on 2026-12-07 and its later
    # pre8 one on 2026-12-08 (#21). Replaying 12-07 before 12-08 is in the lake marks pre7;
    # replaying 12-08 then moves the close to pre8 and rewrites 12-07 (Codex on #189).
    commence = datetime(2026, 12, 8, 1, tzinfo=UTC)
    midday = datetime(2026, 12, 7, 17, 45, 30, tzinfo=UTC)  # 12:45 EST, the decision snapshot
    pre7 = datetime(2026, 12, 7, 23, 45, 30, tzinfo=UTC)
    pre8 = datetime(2026, 12, 8, 0, 45, 30, tzinfo=UTC)
    store = RawStore(tmp_path / "raw")
    for when in (midday, pre7, pre8):
        store_snapshot(store, priced(commence, when), when)
    lake = Lake(tmp_path / "lake")
    after = commence + timedelta(hours=3)
    replay_odds(store, lake, [date(2026, 12, 7)], now=after)
    assert set(lake.read("odds_snapshots")["snapshot_utc"]) == {midday, pre7}
    assert flagged_at(lake) == {pre7}
    report = replay_odds(store, lake, [date(2026, 12, 8)], now=after)
    assert report.reflagged == [date(2026, 12, 7)]
    assert flagged_at(lake) == {pre8}
    sides = lake.read("odds_snapshots").filter("is_closing_proxy")["side"]
    assert sorted(sides.to_list()) == ["away", "home"]
    # Replaying 12-07 again reads 12-08 from the lake: pre8 stays the close.
    assert replay_odds(store, lake, [date(2026, 12, 7)], now=after).reflagged == []
    assert flagged_at(lake) == {pre8}
    # Before the game starts, nothing is marked: a later snapshot could still replace it.
    replay_odds(store, lake, now=commence - timedelta(minutes=1))
    assert flagged_at(lake) == set()


def test_a_game_postponed_for_weeks_loses_its_old_close(tmp_path: Path) -> None:
    # A postponed game keeps its event, however late its new start (Codex on #189): replaying
    # the new date moves its start, and the old date's quotes are no close any more.
    first = datetime(2026, 12, 8, tzinfo=UTC)  # 19:00 EST on 12-07
    later = datetime(2026, 12, 21, tzinfo=UTC)  # 19:00 EST on 12-20, thirteen days on
    old = [
        datetime(2026, 12, 7, 17, 45, 30, tzinfo=UTC),
        datetime(2026, 12, 7, 23, 45, 30, tzinfo=UTC),
    ]
    new = [
        datetime(2026, 12, 20, 17, 45, 30, tzinfo=UTC),
        datetime(2026, 12, 20, 23, 45, 30, tzinfo=UTC),
    ]
    store = RawStore(tmp_path / "raw")
    for when in old:
        store_snapshot(store, priced(first, when), when)
    lake = Lake(tmp_path / "lake")
    replay_odds(store, lake, [date(2026, 12, 7)], now=first + timedelta(hours=3))
    assert flagged_at(lake) == {old[1]}
    for when in new:
        store_snapshot(store, priced(later, when), when)
    report = replay_odds(store, lake, [date(2026, 12, 20)], now=later + timedelta(hours=3))
    assert report.reflagged == [date(2026, 12, 7)]
    assert flagged_at(lake) == {new[1]}
