"""scripts/officials_timing.py (#423): the officials a right-rail page names, each game's first
seen and whether the officials were known by the decision, and the rule, on SYNTHETIC copies."""

import importlib.util
import json
from datetime import UTC, date, datetime, time
from pathlib import Path
from types import ModuleType

import polars as pl
import pytest

from nhl_edge.ingest.pregame import ET
from nhl_edge.lake.raw import RawStore


def load() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "officials_timing.py"
    spec = importlib.util.spec_from_file_location("officials_timing", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ot = load()
DAY = date(2026, 11, 10)


def et(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hour, minute), ET).astimezone(UTC)


def rail(referees: int, linesmen: int = 2) -> bytes:
    """A SYNTHETIC right-rail page naming the given number of officials."""
    person = {"fullName": {"default": "X"}, "sweaterNumber": 1}
    return json.dumps(
        {"gameInfo": {"referees": [person] * referees, "linesmen": [person] * linesmen}}
    ).encode()


def games(*starts: tuple[int, datetime]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "game_id": [g for g, _ in starts],
            "game_date": [s.astimezone(ET).date() for _, s in starts],
            "start_utc": [s for _, s in starts],
            "season": [20262027] * len(starts),
        },
        schema={
            "game_id": pl.Int64,
            "game_date": pl.Date,
            "start_utc": pl.Datetime("us", "UTC"),
            "season": pl.Int32,
        },
    )


def copied(*rows: tuple[int, datetime, int]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "game_id": [g for g, _, _ in rows],
            "fetched_utc": [t for _, t, _ in rows],
            "referees": [r for _, _, r in rows],
            "linesmen": [2 if r else 0 for _, _, r in rows],
        },
        schema={
            "game_id": pl.Int64,
            "fetched_utc": pl.Datetime("us", "UTC"),
            "referees": pl.Int64,
            "linesmen": pl.Int64,
        },
    )


def test_officials_counts_referees_and_linesmen() -> None:
    assert ot.officials(rail(2, 2)) == (2, 2)
    assert ot.officials(rail(0, 0)) == (0, 0)
    assert ot.officials(b"{}") == (0, 0)


EVENING = et(19)
A, B, C, D = 2026020101, 2026020102, 2026020103, 2026020104
MATINEE = 2026020105
PRESEASON = 2026010001


def test_first_seen_known_by_the_decision_matinees_and_coverage() -> None:
    per_game = ot.measure(
        copied(
            # A: named at the morning slot, so known by the decision.
            (A, et(7, 5), 2),
            (A, et(12, 46), 2),
            # B: empty at the morning and midday slots, named at 18:45.
            (B, et(7, 5), 0),
            (B, et(12, 46), 0),
            (B, et(18, 45), 2),
            # C: empty in the morning, no midday copy, named at 18:45: uncovered.
            (C, et(7, 5), 0),
            (C, et(18, 45), 2),
            # D: never named before the start.
            (D, et(7, 5), 0),
            (D, et(12, 46), 0),
            # A matinee starting at noon, named at 07:05.
            (MATINEE, et(7, 5), 2),
            # A preseason game is left out.
            (PRESEASON, et(7, 5), 2),
        ),
        games(
            (A, EVENING),
            (B, EVENING),
            (C, EVENING),
            (D, EVENING),
            (MATINEE, et(12)),
            (PRESEASON, EVENING),
        ),
    )
    rows = {r["game_id"]: r for r in per_game.iter_rows(named=True)}
    assert set(rows) == {A, B, C, D, MATINEE}
    assert rows[A]["known_by_decision"] and rows[A]["covered"]
    assert rows[A]["first_seen_min"] == pytest.approx(11 * 60 + 55)
    assert rows[A]["last_empty_min"] is None
    assert not rows[B]["known_by_decision"] and rows[B]["covered"]
    assert rows[B]["first_seen_min"] == pytest.approx(15)
    assert rows[B]["last_empty_min"] == pytest.approx(6 * 60 + 14)
    assert not rows[C]["covered"]
    assert rows[D]["first_seen_min"] is None and rows[D]["covered"]
    assert rows[MATINEE]["matinee"]
    found = ot.summary(per_game)
    # Covered evening games: A (known), B and D (not); C is uncovered; the matinee is apart.
    assert (found["covered"], found["known_by_decision"], found["uncovered"]) == (3, 1, 1)
    assert found["matinees"] == 1 and found["never_named_before_start"] == 1
    assert found["share_known"] == pytest.approx(1 / 3)
    low, high = found["share_exact_interval"]
    assert low < 1 / 3 < high and high > 0.5  # three games can't settle it
    assert found["outcome"] == "measure on"
    # A at 11h55m, the matinee at 4h55m, and B and C (uncovered, but first seen all the same)
    # at 15 minutes before their starts.
    assert found["first_seen_bands"]["9-12 h"] == 1
    assert found["first_seen_bands"]["3-6 h"] == 1 and found["first_seen_bands"]["0-1 h"] == 2


def test_the_rule_acts_on_the_intervals_never_the_share_alone() -> None:
    # Half the games known, but two games can't settle the rule either way.
    per_game = ot.measure(
        copied((A, et(7, 5), 2), (B, et(12, 46), 0)),
        games((A, EVENING), (B, EVENING)),
    )
    assert ot.summary(per_game)["outcome"] == "measure on"
    assert ot.outcome((0.6, 0.9), (0.55, 0.95)) == "log"
    assert ot.outcome((0.0, 0.0), (0.0, 0.013)) == "close"
    assert ot.outcome((0.0, 0.0), (0.0, 0.6)) == "measure on"


def test_the_exact_interval_stays_informative_when_none_is_known() -> None:
    low, high = ot.exact_interval(0, 287)
    assert low == 0.0 and 0.012 < high < 0.013
    low, high = ot.exact_interval(287, 287)
    assert high == 1.0 and 0.987 < low < 0.988


def test_copies_are_read_through_the_raw_store(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    store.put(
        ot.PREFIX,
        f"2026-11-10/{A}/20261110T120500Z",
        rail(2),
        {"fetched_utc": "2026-11-10T12:05:00Z"},
    )
    found, incomplete = ot.copies(store)
    assert incomplete == []
    assert [(c.game_id, c.referees, c.linesmen) for c in found] == [(A, 2, 2)]
    assert found[0].fetched_utc == datetime(2026, 11, 10, 12, 5, tzinfo=UTC)
