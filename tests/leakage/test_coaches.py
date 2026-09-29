"""Point-in-time rules for coach tenures. A stint's end is future information while it runs, so
features read coaches.csv through coaches_known_at: a stint counts as known from the morning after
its first game, when that game's feeds show who coached it (ADR 0003 and 0004), and its last game
only once the next stint is known.

Team codes, arenas, venues and home arenas are known seasons ahead and need no such rule. A
game's venue reaches features through the schedule (tests/leakage/test_schedule.py)."""

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

import nhl_edge.features
from nhl_edge.ingest.games import result_public_utc
from nhl_edge.reference import (
    KNOWN_COACH_COLUMNS,
    coaches_known_at,
    lineage,
    load_coaches,
    load_teams,
)

COACHES, TEAMS = load_coaches(), load_teams()
SECOND = timedelta(seconds=1)
# Derek Lalonde's last game for DET was on 2024-12-23, Todd McLellan's first on 2024-12-27.
LALONDE_LAST, MCLELLAN_FIRST = date(2024, 12, 23), date(2024, 12, 27)


def detroit(prediction_utc: datetime) -> dict[str, date | None]:
    known = coaches_known_at(COACHES, TEAMS, prediction_utc).filter(pl.col("team") == "DET")
    return dict(known.select("coach", "last_game").iter_rows())


def test_a_new_coach_counts_from_the_morning_after_his_first_game() -> None:
    # On the morning of McLellan's first game, the change may not be public yet.
    game_day = datetime(2024, 12, 27, 11, 5, tzinfo=UTC)
    known = detroit(game_day)
    assert "Todd McLellan" not in known
    # Lalonde still reads as running: his last game would reveal that the change is coming.
    assert known["Derek Lalonde"] is None
    public = result_public_utc(MCLELLAN_FIRST)
    assert "Todd McLellan" not in detroit(public)
    after = detroit(public + SECOND)
    assert after["Todd McLellan"] is None
    assert after["Derek Lalonde"] == LALONDE_LAST


def test_no_known_stint_starts_or_ends_after_the_prediction() -> None:
    for prediction_utc in (
        datetime(2010, 10, 7, 11, 5, tzinfo=UTC),
        datetime(2015, 1, 1, 16, 0, tzinfo=UTC),
        datetime(2021, 3, 8, 11, 5, tzinfo=UTC),
        datetime(2026, 9, 29, 11, 5, tzinfo=UTC),
    ):
        known = coaches_known_at(COACHES, TEAMS, prediction_utc)
        assert known.height > 0
        for first, last in known.select("first_game", "last_game").iter_rows():
            assert result_public_utc(first) < prediction_utc
            assert last is None or result_public_utc(last) < prediction_utc


def test_each_team_has_one_running_stint_once_its_coach_is_known() -> None:
    # Mid-season, every one of the 32 teams has exactly one stint without a known end.
    known = coaches_known_at(COACHES, TEAMS, datetime(2025, 1, 15, 11, 5, tzinfo=UTC))
    running = known.filter(pl.col("last_game").is_null())
    lineages = running["team"].replace_strict(lineage(TEAMS))
    assert lineages.n_unique() == running.height == 32


def test_a_stint_keeps_running_through_a_change_of_code() -> None:
    # André Tourigny's stint, started under ARI, is still Utah's current stint in 2024-25.
    known = coaches_known_at(COACHES, TEAMS, datetime(2025, 1, 15, 11, 5, tzinfo=UTC))
    tourigny = known.filter(pl.col("coach") == "André Tourigny").row(0, named=True)
    assert (tourigny["team"], tourigny["last_game"]) == ("ARI", None)


def test_the_hindsight_note_stays_out() -> None:
    # Notes were written after the fact, such as how the NHL split a shared bench's games.
    known = coaches_known_at(COACHES, TEAMS, datetime(2015, 1, 15, 11, 5, tzinfo=UTC))
    assert (
        tuple(known.columns) == KNOWN_COACH_COLUMNS == ("team", "first_game", "last_game", "coach")
    )


def test_features_read_coaches_only_through_the_selector() -> None:
    # coaches.csv holds every stint's end, so a feature reading it directly would see the future.
    features = Path(nhl_edge.features.__file__).parent
    for module in features.rglob("*.py"):
        source = module.read_text(encoding="utf-8")
        for raw in (r"load_coaches", r"coaches\.csv", r"\.coaches\b"):
            assert not re.search(raw, source), f"{module.name} reads {raw}; use coaches_known_at"
    assert not re.search(r"\.coaches\b", "reference.coaches_known_at(coaches, teams, t)")
