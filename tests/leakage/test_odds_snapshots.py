"""Point-in-time rules for odds snapshots: a prediction may use only quotes observed before it
(snapshot_utc < prediction time), and only for games that have not started by the prediction
time."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from nhl_edge.ingest.odds import available_at, parse_odds

BODY = (
    Path(__file__).parents[1]
    / "unit"
    / "fixtures"
    / "odds_api"
    / "odds_eu_full_20260928T120053Z.json"
).read_bytes()
MORNING = datetime(2026, 9, 28, 12, 0, 53, tzinfo=UTC)
FLORIDA_AT_CAROLINA = datetime(2026, 9, 29, 21, 0, 47, tzinfo=UTC)


def snapshots(*times: datetime) -> pl.DataFrame:
    return pl.concat(parse_odds(BODY, t, "test", f"odds/{t:%H%M%S}") for t in times)


def test_quotes_at_or_after_the_prediction_time_are_excluded() -> None:
    later = MORNING + timedelta(hours=6)
    frame = snapshots(MORNING, later)
    usable = available_at(frame, prediction_utc=later)
    assert usable.height > 0
    assert (usable["snapshot_utc"] < later).all()
    assert available_at(frame, prediction_utc=MORNING).is_empty()


def test_in_play_quotes_are_excluded() -> None:
    in_play = FLORIDA_AT_CAROLINA + timedelta(minutes=30)
    frame = snapshots(in_play)
    usable = available_at(frame, prediction_utc=in_play + timedelta(minutes=1))
    assert (usable["commence_time_utc"] > usable["snapshot_utc"]).all()
    assert FLORIDA_AT_CAROLINA not in usable["commence_time_utc"].to_list()


def test_games_started_by_the_prediction_time_are_excluded() -> None:
    # Both morning quotes are pre-game, but Carolina has started by the prediction time, so its
    # price is no longer executable. Toronto starts at 23:10 and stays.
    frame = snapshots(MORNING)
    usable = available_at(frame, prediction_utc=FLORIDA_AT_CAROLINA + timedelta(minutes=1))
    assert set(usable["home"]) == {"TOR"}


def test_the_lake_history_keeps_each_quote_when_it_was_observed(tmp_path: Path) -> None:
    # nhl odds replay rebuilds the history from the raw snapshot: every quote keeps the snapshot
    # time, so the same filter gives a prediction only quotes it could have seen.
    from nhl_edge.ingest.odds_lake import replay_odds
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.tables import Lake

    store = RawStore(tmp_path / "raw")
    meta = {"fetched_utc": MORNING.isoformat(), "slot": "morning"}
    store.put("odds", "2026-09-28/snap", BODY, meta)
    lake = Lake(tmp_path / "lake")
    replay_odds(store, lake)
    history = lake.read("odds_snapshots")
    assert (history["snapshot_utc"] == MORNING).all()
    assert available_at(history, prediction_utc=MORNING).is_empty()
    later = FLORIDA_AT_CAROLINA + timedelta(minutes=1)
    assert set(available_at(history, prediction_utc=later)["home"]) == {"TOR"}
