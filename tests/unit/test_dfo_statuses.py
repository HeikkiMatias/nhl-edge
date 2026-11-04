"""scripts/dfo_statuses.py (#121 step 3): the snapshot before the decision, Daily Faceoff's
groups, the match to the candidates, and the leave-one-week-out probability, on made-up rows."""

import importlib.util
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType

import polars as pl
import pytest

D = date(2026, 10, 20)
DECISION = datetime(2026, 10, 20, 17, 45, tzinfo=UTC)
UTC_TYPE = pl.Datetime("us", "UTC")


def load() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "dfo_statuses.py"
    spec = importlib.util.spec_from_file_location("dfo_statuses", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ds = load()


def page(
    rows: list[tuple[int, str, int | None, str, str | None]], fetched: datetime
) -> pl.DataFrame:
    """SYNTHETIC page rows: (dfo id, name, jersey, group, status), all BOS on D."""
    return pl.DataFrame(
        {
            "game_date": [D] * len(rows),
            "team": ["BOS"] * len(rows),
            "dfo_player_id": [r[0] for r in rows],
            "player_name": [r[1] for r in rows],
            "jersey": [r[2] for r in rows],
            "group": [r[3] for r in rows],
            "injury_status": [r[4] for r in rows],
            "observed_utc": [fetched] * len(rows),
        },
        schema_overrides={"jersey": pl.Int16, "injury_status": pl.String, "observed_utc": UTC_TYPE},
    )


def test_the_snapshot_is_the_last_page_fetched_before_the_decision() -> None:
    early = page([(1, "A B", 1, "f1", None)], DECISION - timedelta(hours=2))
    last = page([(1, "A B", 1, "ir", "out")], DECISION - timedelta(minutes=1))
    after = page([(1, "A B", 1, "f1", None)], DECISION)
    decisions = pl.DataFrame({"game_date": [D], "prediction_utc": [DECISION]}).with_columns(
        pl.col("prediction_utc").cast(UTC_TYPE)
    )
    found = ds.snapshots(pl.concat([early, last, after]), decisions)
    assert found["group"].to_list() == ["ir"]


def test_each_player_takes_the_first_group_that_applies() -> None:
    rows = page(
        [
            (1, "Out Player", 1, "f1", "out"),
            (2, "Ir Player", 2, "ir", None),
            (3, "Dtd Player", 3, "d1", "dtd"),
            (3, "Dtd Player", 3, "pp1", None),
            (4, "Line Player", 4, "f2", None),
            (4, "Line Player", 4, "pk1", None),
            (5, "Unit Player", 5, "pp2", None),
        ],
        DECISION - timedelta(minutes=1),
    )
    found = dict(ds.page_groups(rows).select("dfo_player_id", "dfo_group").iter_rows())
    assert found == {1: "out", 2: "out", 3: "dtd", 4: "lines", 5: "other"}


def test_names_are_normalized_without_accents_or_punctuation() -> None:
    frame = pl.DataFrame({"n": ["Tim Stützle", "J.T. Miller", "Pierre-Luc Dubois"]})
    found = frame.select(full=ds.normalized(pl.col("n"))).with_columns(
        last=ds.last_name(pl.col("full"))
    )
    assert found["full"].to_list() == ["tim stutzle", "jt miller", "pierreluc dubois"]
    assert found["last"].to_list() == ["stutzle", "miller", "dubois"]


def test_a_candidate_is_matched_by_jersey_and_last_name_or_else_by_full_name() -> None:
    groups = ds.page_groups(
        page(
            [
                (1, "David Pastrnak", 88, "f1", None),
                (2, "Charlie McAvoy", None, "ir", "out"),
                (3, "Brad Marchand", 63, "f2", None),
            ],
            DECISION - timedelta(minutes=1),
        )
    )
    cands = pl.DataFrame(
        {
            "game_id": [1, 1, 1, 1],
            "game_date": [D] * 4,
            "team": ["BOS"] * 4,
            "player_id": [10, 20, 30, 40],
            # 30 wears 63 but is someone else: no match by jersey, nor by name.
            "jersey": [88, 73, 63, 11],
            "full": ["david pastrnak", "charlie mcavoy", "someone else", "trent frederic"],
        },
        schema_overrides={"jersey": pl.Int16},
    ).with_columns(last=ds.last_name(pl.col("full")))
    found = ds.matched(cands, groups).sort("player_id")
    assert found["dfo_group"].to_list() == ["lines", "out", "absent", "absent"]
    assert found["how"].to_list() == ["jersey", "name", None, None]


def test_the_informed_probability_reads_only_earlier_weeks() -> None:
    week = [date(2026, 10, 12), date(2026, 10, 19)]
    as_of = [datetime(2026, 10, 13, 14, tzinfo=UTC), datetime(2026, 10, 20, 14, tzinfo=UTC)]
    rows = pl.DataFrame(
        {
            "season": [20262027] * 4,
            "game_date": [week[0], week[0], week[1], week[1]],
            "week": [week[0], week[0], week[1], week[1]],
            "as_of_utc": [as_of[0], as_of[0], as_of[1], as_of[1]],
            "label_utc": [a + timedelta(days=1) for a in (as_of[0], as_of[0], as_of[1], as_of[1])],
            "dfo_group": ["out", "lines", "out", "lines"],
            "dressed": [0.0, 1.0, 1.0, 1.0],
            "p_available": [0.6, 0.9, 0.6, 0.9],
        }
    ).with_columns(pl.col("as_of_utc", "label_utc").cast(UTC_TYPE))
    found = ds.informed(rows).sort("game_date", "dfo_group")
    # The first week has no history: everyone keeps p_available. The second week's out player
    # takes the first week's out share, 0.0. Players in the lines keep p_available.
    assert found["p_informed"].to_list() == pytest.approx([0.9, 0.6, 0.9, 0.0])
