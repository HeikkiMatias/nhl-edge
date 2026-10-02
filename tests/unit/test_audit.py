import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from test_reference import OPENING
from typer.testing import CliRunner

from nhl_edge.audit import games as game_audit
from nhl_edge.audit import snapshots as snapshot_audit
from nhl_edge.cli import app
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.tables import Lake

FIXTURES = Path(__file__).parent / "fixtures" / "nhl_api"
WEEK_2010 = (FIXTURES / "schedule_2010-10-07.json").read_bytes()
WEEK_2026 = (FIXTURES / "schedule_2026-09-28.json").read_bytes()
# OPENING's games: MIN and CAR in Helsinki on 2010-10-07 and 2010-10-08, CHI at COL on 2010-10-07.
HELSINKI, CHI_AT_COL, HELSINKI_REMATCH = 2010020003, 2010020004, 2010020008
OPENING_DAY, LAST_LISTED = date(2010, 10, 7), date(2010, 10, 8)


def listed_2010(tmp_path: Path) -> pl.DataFrame:
    store = RawStore(tmp_path / "raw")
    store.put("nhl", "schedule/2010-10-07/20101015T100000Z", WEEK_2010, {"fetched_utc": "x"})
    return game_audit.listed_games(store)


def season_row(games: pl.DataFrame, listed: pl.DataFrame, as_of: date) -> dict[str, Any]:
    return game_audit.season_report(games, listed, as_of).row(0, named=True)


def test_each_game_counts_at_its_newest_listing(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    store.put("nhl", "schedule/2026-09-28/20260928T120000Z", WEEK_2026, {"fetched_utc": "x"})
    later = json.loads(WEEK_2026)
    later["gameWeek"][1]["games"][0]["gameState"] = "OFF"  # FLA at CAR, 2026020001
    store.put("nhl", "schedule/2026-09-29/20260930T090000Z", json.dumps(later).encode(), {"x": 1})
    listed = game_audit.listed_games(store)
    assert listed["game_id"].is_unique().all()
    states = dict(listed.select("game_id", "game_state").iter_rows())
    assert states[2026020001] == "OFF"
    assert states[2026020002] == "FUT"


def test_a_short_season_and_its_number_gaps_are_problems(tmp_path: Path) -> None:
    # The trimmed fixture lists the season through 2010-10-08, so by then it counts as over.
    listed = listed_2010(tmp_path)
    row = season_row(OPENING, listed, LAST_LISTED)
    assert (row["games"], row["expected"], row["teams"], row["number_gaps"]) == (3, 1230, 4, 5)
    assert [row[name] for name in game_audit.COUNTS] == [0, 0, 0, 0, 0]
    report = game_audit.season_report(OPENING, listed, LAST_LISTED)
    assert game_audit.problems(report, OPENING, listed, LAST_LISTED) == [
        "20102011: 3 games, expected 1230",
        "20102011: 5 game numbers missing below the highest",
    ]


def test_a_game_entered_twice_is_found(tmp_path: Path) -> None:
    listed = listed_2010(tmp_path)
    copy = OPENING.filter(pl.col("game_id") == CHI_AT_COL).with_columns(
        game_id=pl.lit(2010020009, pl.Int64)
    )
    twice = pl.concat([OPENING, copy])
    row = season_row(twice, listed, OPENING_DAY)
    # CHI and COL each play twice on 2010-10-07; the copy is in no listing.
    assert (row["team_twice_a_day"], row["repeated_matchups"], row["not_listed"]) == (2, 1, 1)
    report = game_audit.season_report(twice, listed, OPENING_DAY)
    found = game_audit.problems(report, twice, listed, OPENING_DAY)
    assert "20102011: 1 games final but in no listing, e.g. 2010020009" in found
    assert "20102011: 1 matchups repeated on one date" in found


def test_a_listed_game_is_missing_only_once_its_date_is_covered(tmp_path: Path) -> None:
    listed = listed_2010(tmp_path)
    games = OPENING.filter(pl.col("game_id") != HELSINKI_REMATCH)  # dated 2010-10-08
    assert season_row(games, listed, OPENING_DAY)["missing"] == 0
    assert season_row(games, listed, date(2010, 10, 8))["missing"] == 1
    report = game_audit.season_report(games, listed, date(2010, 10, 8))
    assert (
        "20102011: 1 games listed by the audit date but not in games, e.g. 2010020008 (OFF)"
        in game_audit.problems(report, games, listed, date(2010, 10, 8))
    )


def test_an_expected_season_with_no_games_is_found_once_over(tmp_path: Path) -> None:
    listed = listed_2010(tmp_path)
    # 2011-12 has no games and no listing: over from July 1, 2012, the day it could not reach.
    before = game_audit.season_report(OPENING, listed, date(2012, 6, 30))
    assert 20112012 not in before["season"].to_list()
    report = game_audit.season_report(OPENING, listed, date(2012, 7, 1))
    row = report.filter(pl.col("season") == 20112012).row(0, named=True)
    assert (row["games"], row["expected"], row["teams"]) == (0, 1230, 0)
    assert "20112012: 0 games, expected 1230" in game_audit.problems(
        report, OPENING, listed, date(2012, 7, 1)
    )


def test_a_season_under_way_is_not_held_to_its_length(tmp_path: Path) -> None:
    # Listed through 2010-10-08, so on 2010-10-07 the season is under way: no count or gap check.
    listed = listed_2010(tmp_path)
    games = OPENING.filter(pl.col("game_date") <= date(2010, 10, 7))
    report = game_audit.season_report(games, listed, date(2010, 10, 7))
    assert report.row(0, named=True)["expected"] is None
    assert game_audit.problems(report, games, listed, date(2010, 10, 7)) == []


def test_a_game_dated_differently_from_its_listing_is_found(tmp_path: Path) -> None:
    listed = listed_2010(tmp_path)
    moved = OPENING.with_columns(
        game_date=pl.when(pl.col("game_id") == HELSINKI)
        .then(date(2010, 10, 6))
        .otherwise(pl.col("game_date"))
    )
    assert season_row(moved, listed, OPENING_DAY)["date_differs"] == 1


def put_run(store: RawStore, slot: str, fetched: datetime, last: int, remaining: int) -> str:
    return store.put(
        "odds",
        f"{fetched:%Y-%m-%d}/{fetched:%Y%m%dT%H%M%SZ}_{slot}_eu",
        b"[]",
        {
            "fetched_utc": fetched.isoformat(),
            "slot": slot,
            "credits": {"last": last, "remaining": remaining, "used": 500 - remaining},
        },
    )


def test_a_run_belongs_to_the_et_day_of_its_slot(tmp_path: Path) -> None:
    store = RawStore(tmp_path / "raw")
    put_run(store, "morning", datetime(2026, 9, 29, 11, 20, tzinfo=UTC), 3, 480)  # 07:20 EDT
    put_run(store, "pre10", datetime(2026, 9, 30, 2, 10, tzinfo=UTC), 1, 479)  # 22:10 EDT
    put_run(store, "pre10", datetime(2026, 9, 30, 4, 30, tzinfo=UTC), 1, 478)  # 00:30 EDT
    runs = snapshot_audit.slot_runs(store)
    assert runs.select("slot", "slot_day", "delay_min").rows() == [
        ("morning", date(2026, 9, 29), 15.0),
        ("pre10", date(2026, 9, 29), 25.0),
        ("pre10", date(2026, 9, 29), 165.0),  # late past midnight: still the 29th's slot
    ]
    credits = snapshot_audit.credit_report(runs)
    assert credits.rows() == [(date(2026, 9, 29), 3, 5, 478)]


def schedule(*starts: datetime, game_type: int = 2) -> pl.DataFrame:
    """Listed games (as audit.games.listed_games gives them) starting at the given times."""
    return pl.DataFrame(
        {
            "game_id": [2026020001 + i for i in range(len(starts))],
            "game_type": [game_type] * len(starts),
            "start_utc": list(starts),
            "home": ["CAR"] * len(starts),
            "away": ["FLA"] * len(starts),
        },
        schema={
            "game_id": pl.Int64,
            "game_type": pl.Int8,
            "start_utc": pl.Datetime("us", "UTC"),
            "home": pl.String,
            "away": pl.String,
        },
    )


def due_on(frame: pl.DataFrame, day: date) -> dict[str, bool]:
    return dict(frame.filter(pl.col("slot_day") == day).select("slot", "due").iter_rows())


def test_a_slot_is_due_when_the_job_would_have_priced_a_game() -> None:
    day = date(2026, 9, 29)
    # One game at 17:00 EDT: the all-day slots are due, the pre-game slots come after its start.
    early = snapshot_audit.due_slots(schedule(datetime(2026, 9, 29, 21, tzinfo=UTC)), [day])
    assert due_on(early, day) == {
        "morning": True,
        "midday": True,
        "pre7": False,
        "pre8": False,
        "pre10": False,
    }
    # A 22:00 EDT start is within 90 minutes of pre10 (21:45) only.
    late = snapshot_audit.due_slots(schedule(datetime(2026, 9, 30, 2, tzinfo=UTC)), [day])
    assert due_on(late, day)["pre8"] is False
    assert due_on(late, day)["pre10"] is True
    idle = snapshot_audit.due_slots(
        schedule(datetime(2026, 9, 29, 21, tzinfo=UTC)), [day.replace(day=28)]
    )
    assert not any(due_on(idle, date(2026, 9, 28)).values())


def test_playoff_games_make_slots_due_and_preseason_games_do_not() -> None:
    day, start = date(2027, 4, 20), datetime(2027, 4, 20, 23, tzinfo=UTC)  # 19:00 EDT
    playoffs = snapshot_audit.due_slots(schedule(start, game_type=3), [day])
    assert due_on(playoffs, day)["morning"] is True
    assert due_on(playoffs, day)["pre7"] is True
    preseason = snapshot_audit.due_slots(schedule(start, game_type=1), [day])
    assert not any(due_on(preseason, day).values())


def runs_frame(*rows: tuple[str, date, float]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "raw_key": f"odds/{day}/{slot}",
                "slot": slot,
                "fetched_utc": datetime(2026, 9, 29, tzinfo=UTC),
                "slot_day": day,
                "delay_min": delay,
                "credits_last": 1,
                "credits_remaining": 400,
            }
            for slot, day, delay in rows
        ],
        schema=snapshot_audit.RUNS_SCHEMA,
    )


def test_missed_and_late_slots_are_problems_and_manual_runs_are_not() -> None:
    game_day, idle_day = date(2026, 9, 29), date(2026, 9, 28)
    due = snapshot_audit.due_slots(
        schedule(datetime(2026, 9, 29, 23, tzinfo=UTC)), [idle_day, game_day]
    )
    runs = runs_frame(
        ("morning", idle_day, 56.0),  # a manual run on a day without games
        ("morning", game_day, 15.0),
        ("morning", game_day, 95.0),  # a retry after the on-time run: not late
        ("pre7", game_day, 30.0),
        ("pre7", game_day, 50.0),  # a second late run: one problem per slot day
    )
    report = {
        row["slot"]: row for row in snapshot_audit.slot_report(runs, due).iter_rows(named=True)
    }
    assert [report["morning"][k] for k in ("due", "landed", "missed", "not_due_runs")] == [
        1,
        1,
        0,
        1,
    ]
    assert report["morning"]["delay_max_min"] == 15.0
    assert [report["midday"][k] for k in ("due", "landed", "missed")] == [1, 0, 1]
    assert report["pre7"]["delay_max_min"] == 30.0
    no_events = snapshot_audit.event_matches(pl.DataFrame(schema=EVENT_COLUMNS))
    assert snapshot_audit.problems(runs, due, no_events, game_day) == [
        "2026-09-29 midday: due, no snapshot stored",
        "2026-09-29 pre7: stored 30 minutes after its slot time (odds/2026-09-29/pre7)",
    ]


EVENT_COLUMNS = {
    "snapshot_utc": pl.Datetime("us", "UTC"),
    "event_id": pl.String,
    "home": pl.String,
    "away": pl.String,
    "commence_time_utc": pl.Datetime("us", "UTC"),
    "game_id": pl.Int64,
    "game_type": pl.Int8,
}


def test_an_unmatched_event_is_a_problem_once_its_game_day_is_covered() -> None:
    snapshot = datetime(2026, 9, 28, 12, tzinfo=UTC)
    odds = pl.DataFrame(
        [
            (snapshot, "a", "CAR", "FLA", datetime(2026, 9, 29, 21, tzinfo=UTC), 2026020001, 2),
            (snapshot, "b", "CHI", "STL", datetime(2026, 9, 30, 0, 10, tzinfo=UTC), None, None),
            (snapshot, "c", "OTT", "PHI", datetime(2026, 10, 8, 23, 10, tzinfo=UTC), None, None),
        ],
        schema=EVENT_COLUMNS,
        orient="row",
    )
    events = snapshot_audit.event_matches(odds)
    none_due = pl.DataFrame(schema={"slot_day": pl.Date, "slot": pl.String, "due": pl.Boolean})
    runs = runs_frame()
    # b starts at 20:10 EDT on 2026-09-29; c on 2026-10-08 can still match a later listing.
    assert snapshot_audit.problems(runs, none_due, events, date(2026, 9, 29)) == [
        "event b (STL at CHI, 2026-09-30 00:10 UTC) matches no NHL game"
    ]


def test_quote_age_counts_moneyline_quotes_by_slot() -> None:
    snapshot = datetime(2026, 9, 29, 22, 45, tzinfo=UTC)
    odds = pl.DataFrame(
        [
            (snapshot, datetime(2026, 9, 29, 22, 45, tzinfo=UTC), "h2h", "pinnacle", "pre7"),
            (snapshot, datetime(2026, 9, 29, 20, 45, tzinfo=UTC), "h2h", "coolbet", "pre7"),
            (snapshot, datetime(2026, 9, 28, 20, 45, tzinfo=UTC), "totals", "coolbet", "pre7"),
        ],
        schema={
            "snapshot_utc": pl.Datetime("us", "UTC"),
            "last_update_utc": pl.Datetime("us", "UTC"),
            "market": pl.String,
            "book": pl.String,
            "slot": pl.String,
        },
        orient="row",
    )
    ages = {row["books"]: row for row in snapshot_audit.quote_age(odds).iter_rows(named=True)}
    assert (ages["all"]["quotes"], ages["all"]["age_max_min"], ages["all"]["over_an_hour"]) == (
        2,
        120.0,
        0.5,
    )
    assert (ages["pinnacle"]["quotes"], ages["pinnacle"]["over_an_hour"]) == (1, 0.0)


runner = CliRunner()


def test_audit_report_writes_every_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    Lake().write("games", OPENING)
    RawStore().put("nhl", "schedule/2010-10-07/20101015T100000Z", WEEK_2010, {"x": 1})
    result = runner.invoke(app, ["audit", "report", "--as-of", "2010-10-08"])
    assert result.exit_code == 0, result.output
    assert "Games per season: 2" in result.output
    text = (tmp_path / "reports" / "audit" / "2010-10-08.md").read_text()
    for title in (
        "Games per season",
        "SBR odds",
        "Shift coverage",
        "Penalties and faceoffs",
        "Player league seasons",
        "Reference files",
        "Live odds snapshots",
        "Starting goalies",
    ):
        assert f"\n## {title}\n" in text
    assert "- 20102011: 3 games, expected 1230" in text
    assert "- no odds snapshots" in text
    # A report to an earlier date leaves out the games after it.
    earlier = runner.invoke(app, ["audit", "report", "--as-of", "2010-10-07"])
    assert earlier.exit_code == 0, earlier.output
    text = (tmp_path / "reports" / "audit" / "2010-10-07.md").read_text()
    assert "| 20102011 | 2 | under way |" in text


def test_audit_report_needs_games_and_a_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    empty = runner.invoke(app, ["audit", "report"])
    assert empty.exit_code == 1
    assert "no games in the lake" in empty.output
    Lake().write("games", OPENING)
    bad = runner.invoke(app, ["audit", "report", "--as-of", "yesterday"])
    assert bad.exit_code == 2
