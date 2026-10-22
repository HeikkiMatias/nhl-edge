"""Leakage guard for the challengers (#255, ADR 0038): a challenger reads the lake only point in
time, through a reviewed reader for each table it registered, at its bundle's input cutoff; and
no decision day on or before its registration is ever computed. conservative_blend (ADR 0041)
trains only on earlier weeks' v2 decisions published, and results public, before the cutoff."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl
import pytest

from nhl_edge.betting.selection import POLICY_V1, POLICY_V2
from nhl_edge.ingest.games import result_public_utc
from nhl_edge.live import bundle as lb
from nhl_edge.live import challengers as lc

CUTOFF = datetime(2026, 10, 16, 16, 45, 30, tzinfo=UTC)
SECOND = timedelta(seconds=1)
UTC_US = pl.Datetime("us", "UTC")


class Lake:
    """A lake of one table per name."""

    def __init__(self, **tables: pl.DataFrame) -> None:
        self.tables = tables

    def read(self, table: str, seasons: Any = None) -> pl.DataFrame:
        return self.tables[table]


def test_the_odds_are_read_as_they_were_before_the_cutoff() -> None:
    # Quotes observed just before, at and just after the cutoff, with the columns set
    # afterwards: the closing proxy flag and the game a quote was matched to (Codex on #265).
    odds = pl.DataFrame(
        {
            "event_id": ["e1", "e1", "e1"],
            "snapshot_utc": [CUTOFF - SECOND, CUTOFF, CUTOFF + SECOND],
            "price_decimal": [1.9, 1.95, 2.0],
            "is_closing_proxy": [True, False, False],
            "game_id": [2026020101] * 3,
            "game_type": [2] * 3,
        },
        schema_overrides={"snapshot_utc": UTC_US, "game_type": pl.Int8},
    )
    reader = lc.PointInTime(Lake(odds_snapshots=odds), CUTOFF, ("odds_snapshots",))
    read = reader.read("odds_snapshots")
    assert read["price_decimal"].to_list() == [1.9]
    assert not set(lc.ODDS_AFTERWARDS) & set(read.columns)
    assert list(reader.reads) == ["odds_snapshots"]


def test_the_schedule_holds_only_played_games_and_the_slates_own() -> None:
    # The schedule holds only games that went on to be played (Codex on #265): an upcoming game
    # outside the slate, public or not, would reveal whether it was postponed.
    day = CUTOFF.date()
    played, slate, upcoming = 2026020090, 2026020101, 2026020120
    schedule = pl.DataFrame(
        {
            "game_id": [played, slate, upcoming],
            "game_date": [day - timedelta(days=1), day, day + timedelta(days=1)],
            "observed_utc": [CUTOFF - timedelta(days=3)] * 3,
        },
        schema_overrides={"observed_utc": UTC_US},
    )
    assert result_public_utc(day - timedelta(days=1)).replace(tzinfo=UTC) < CUTOFF
    reader = lc.PointInTime(Lake(schedule=schedule), CUTOFF, ("schedule",), (slate,))
    assert sorted(reader.read("schedule")["game_id"].to_list()) == [played, slate]


def test_results_are_read_only_once_public() -> None:
    games = pl.DataFrame(
        {"game_id": [1, 2, 3], "observed_utc": [CUTOFF - SECOND, CUTOFF, CUTOFF + SECOND]},
        schema_overrides={"observed_utc": UTC_US},
    )
    reader = lc.PointInTime(Lake(games=games), CUTOFF, ("games",))
    assert reader.read("games")["game_id"].to_list() == [1]


def test_a_table_is_read_only_when_registered_and_reviewed() -> None:
    lake = Lake(games=pl.DataFrame(), goalie_starts=pl.DataFrame())
    with pytest.raises(ValueError, match="not among the tables"):
        lc.PointInTime(lake, CUTOFF, ()).read("games")
    # A table without a reviewed reader can't be read, nor registered.
    with pytest.raises(ValueError, match="no point-in-time reader"):
        lc.PointInTime(lake, CUTOFF, ("goalie_starts",)).read("goalie_starts")
    with pytest.raises(ValueError, match="no point-in-time reader for goalie_starts"):
        replace(lc.REGISTRY[0], reads=("goalie_starts",))
    for challenger in lc.REGISTRY:
        assert set(challenger.reads) <= set(lc.READERS)


def test_no_day_on_or_before_the_registration_is_ever_computed() -> None:
    challenger = replace(lc.REGISTRY[0], registered_on=date(2026, 10, 15))
    assert not challenger.counts(date(2026, 10, 14))
    assert not challenger.counts(date(2026, 10, 15))
    assert challenger.counts(date(2026, 10, 16))

    class Store:
        def keys(self, prefix: str) -> list[str]:
            if prefix != "bundles/live":
                return []
            days = ("2026-10-14", "2026-10-15", "2026-10-16")
            return [f"bundles/live/{day}/manifest.json" for day in days]

    assert lc.due(Store(), (challenger,)) == [(challenger, date(2026, 10, 16))]  # type: ignore[arg-type]

    on_the_day = lb.Bundle({"day": "2026-10-15"}, {}, None, None)
    with pytest.raises(ValueError, match="never its evidence"):
        lc.run(challenger, on_the_day, "x", None, Lake(), "x", CUTOFF + SECOND)  # type: ignore[arg-type]


def test_the_ledgers_are_read_only_as_published_before_the_cutoff() -> None:
    # Decisions published just before, at and just after the cutoff: a ledger is written once
    # and has no column set after its publication.
    ledger = pl.DataFrame(
        {
            "game_date": [CUTOFF.date()] * 3,
            "game_id": [3, 1, 2],
            "published_utc": [CUTOFF - SECOND, CUTOFF - 2 * SECOND, CUTOFF + SECOND],
        },
        schema_overrides={"published_utc": UTC_US},
    )
    reader = lc.PointInTime(Lake(paper_ledger=ledger), CUTOFF, ("paper_ledger",))
    assert reader.read("paper_ledger")["game_id"].to_list() == [1, 3]


def test_conservative_blend_trains_only_on_earlier_weeks_published_v2_decisions() -> None:
    # The cutoff is Friday 2026-10-16's; its ET week starts on Monday 10-12.
    day, monday = CUTOFF.date(), date(2026, 10, 12)
    assert lc.week_of(day) == monday

    def start(on: date) -> datetime:
        return datetime(on.year, on.month, on.day, 23, tzinfo=UTC)

    earlier = monday - timedelta(days=2)
    decisions = {
        # game_id: (decided for, its policy, its status, published)
        1: (earlier, POLICY_V2, "predicted", None),  # trained on
        2: (monday, POLICY_V2, "predicted", None),  # the day's own week
        3: (earlier, POLICY_V1, "predicted", None),  # v1's B3 read no confirmed starters
        4: (earlier, POLICY_V2, "stale", None),  # no prediction
        5: (earlier, POLICY_V2, "predicted", CUTOFF),  # published at the cutoff
        6: (earlier - timedelta(days=1), POLICY_V2, "predicted", None),  # postponed...
        7: (earlier, POLICY_V2, "predicted", None),  # its result public only later
        8: (earlier, POLICY_V2, "predicted", None),  # no result yet
    }
    rows = [
        {
            "game_date": on,
            "season": 20262027,
            "game_id": game_id,
            "start_utc": start(on),
            "published_utc": published or start(on) - timedelta(hours=6),
            "status": status,
            "policy_version": policy,
            "p_b0": 0.5,
            "p_b3": 0.55,
        }
        for game_id, (on, policy, status, published) in decisions.items()
    ]
    # ...and decided again on its new date, two days later: only the decision it was played on.
    rows.append({**rows[5], "game_date": earlier + timedelta(days=1)})
    rows[-1]["start_utc"] = start(earlier + timedelta(days=1))
    ledger = pl.DataFrame(rows, schema_overrides={"season": pl.Int32})
    # Every game but 8 has been played, 6 on its new date; 7's result is public only after the
    # cutoff.
    played = {row["game_id"]: row["start_utc"] for row in rows if row["game_id"] != 8}
    public = {game_id: at + timedelta(hours=11) for game_id, at in played.items()}
    public[7] = CUTOFF + SECOND
    games = pl.DataFrame(
        {
            "game_id": list(played),
            "season": [20262027] * len(played),
            "start_utc": list(played.values()),
            "home_score": [3] * len(played),
            "away_score": [2] * len(played),
            "observed_utc": [public[game_id] for game_id in played],
        },
        schema_overrides={"season": pl.Int32, "home_score": pl.Int16, "away_score": pl.Int16},
    )
    lake = lc.PointInTime(Lake(paper_ledger=ledger, games=games), CUTOFF, lc.REGISTRY[1].reads)
    trained = lc.conservative_training(day, [20262027], lake)
    assert trained.select("game_id", "game_date").rows() == [
        (1, earlier),
        (6, earlier + timedelta(days=1)),
    ]
    assert trained["home_win"].to_list() == [1, 1]
