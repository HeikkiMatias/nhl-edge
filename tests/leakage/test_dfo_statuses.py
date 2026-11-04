"""Point in time for scripts/dfo_statuses.py (#121 step 3, hard rules 1 and 9): the page read is
fetched before the decision, a jersey comes from a boxscore public before the candidate's as-of
time, and the informed probability reads only the other weeks' outcomes."""

import importlib.util
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

D = date(2026, 10, 20)
DECISION = datetime(2026, 10, 20, 17, 45, tzinfo=UTC)
UTC_TYPE = pl.Datetime("us", "UTC")
_spec = importlib.util.spec_from_file_location(
    "dfo_statuses", Path(__file__).parents[2] / "scripts" / "dfo_statuses.py"
)
assert _spec is not None and _spec.loader is not None
ds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ds)


def test_a_page_fetched_at_or_after_the_decision_is_never_read() -> None:
    def page(fetched: datetime, group: str) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "game_date": [D],
                "team": ["BOS"],
                "dfo_player_id": [1],
                "player_name": ["A B"],
                "jersey": [1],
                "group": [group],
                "injury_status": [None],
                "observed_utc": [fetched],
            },
            schema_overrides={
                "jersey": pl.Int16,
                "injury_status": pl.String,
                "observed_utc": UTC_TYPE,
            },
        )

    decisions = pl.DataFrame({"game_date": [D], "prediction_utc": [DECISION]}).with_columns(
        pl.col("prediction_utc").cast(UTC_TYPE)
    )
    both = pl.concat([page(DECISION - timedelta(hours=1), "f1"), page(DECISION, "ir")])
    assert ds.snapshots(both, decisions)["group"].to_list() == ["f1"]
    assert ds.snapshots(page(DECISION, "ir"), decisions).is_empty()


def test_a_jersey_comes_from_a_boxscore_public_before_the_as_of_time() -> None:
    as_of = datetime(2026, 10, 20, 14, tzinfo=UTC)
    actual = pl.DataFrame(
        {
            "player_id": [10, 10, 10],
            "sweater_number": [11, 22, 33],
            "observed_utc": [as_of - timedelta(days=2), as_of - timedelta(days=1), as_of],
        }
    ).with_columns(pl.col("observed_utc").cast(UTC_TYPE))
    cands = pl.DataFrame(
        {"game_id": [1], "team": ["BOS"], "player_id": [10], "as_of_utc": [as_of]}
    ).with_columns(pl.col("as_of_utc").cast(UTC_TYPE))
    # The boxscore public at the as-of time itself is not yet known: his jersey is 22.
    assert ds.jerseys(actual, cands)["jersey"].to_list() == [22]


def test_the_informed_probability_never_reads_its_own_weeks_outcomes() -> None:
    weeks = [date(2026, 10, 12), date(2026, 10, 19)]
    rows = pl.DataFrame(
        {
            "season": [20262027] * 3,
            "game_date": [weeks[0], weeks[1], weeks[1]],
            "week": [weeks[0], weeks[1], weeks[1]],
            "dfo_group": ["out"] * 3,
            "dressed": [0.0, 1.0, 1.0],
            "p_available": [0.5] * 3,
        }
    )
    flipped = rows.with_columns(
        dressed=pl.when(pl.col("week") == weeks[0]).then(1.0).otherwise(pl.col("dressed"))
    )
    first = ds.informed(rows).filter(pl.col("week") == weeks[0])["p_informed"]
    # Changing week 1's own outcome leaves its informed probability as it was.
    assert ds.informed(flipped).filter(pl.col("week") == weeks[0])["p_informed"].equals(first)
