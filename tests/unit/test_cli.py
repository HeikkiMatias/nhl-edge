import re
from typing import Any

import pytest
from typer.testing import CliRunner

from nhl_edge.cli import app
from nhl_edge.lake.r2 import R2_ENV, R2Config

runner = CliRunner()


def plain(text: str) -> str:
    """Output without ANSI styling, which Rich adds on CI runners."""
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", text).split())


COMMANDS = [
    "ingest",
    "rate",
    "predict",
    "backtest",
    "xg",
    "stints",
    "team-strength",
    "goalie-start",
    "goalie-effect",
    "schedule-terms",
    "bets",
    "odds",
    "lake",
    "audit",
    "status",
]
STUBS = [
    ["rate"],
    ["predict"],
    ["bets"],
    ["odds", "backfill"],
]
SNAPSHOT = ["odds", "snapshot", "--regions", "eu", "--slot-plan", "free-tier", "--skip-if-no-games"]


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in COMMANDS:
        assert command in result.output


@pytest.mark.parametrize("args", STUBS, ids=lambda args: " ".join(args[:2]))
def test_stubs_fail_loudly(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert "not implemented yet" in result.output


@pytest.mark.parametrize("extra", [[], ["--cron", "45 23 * * *", "--slot", "pre7"]])
def test_snapshot_needs_exactly_one_of_cron_and_slot(extra: list[str]) -> None:
    result = runner.invoke(app, [*SNAPSHOT, *extra])
    assert result.exit_code == 2
    assert "exactly one of --cron and --slot" in plain(result.output)


def test_snapshot_rejects_unknown_slot_and_plan() -> None:
    assert runner.invoke(app, [*SNAPSHOT, "--slot", "noon"]).exit_code == 2
    assert (
        runner.invoke(app, ["odds", "snapshot", "--slot-plan", "paid", "--slot", "pre7"]).exit_code
        == 2
    )


def test_cron_outside_every_slot_exits_quietly_before_any_call() -> None:
    # 03:00 UTC is 23:00 EDT or 22:00 EST: never a slot.
    result = runner.invoke(app, [*SNAPSHOT, "--cron", "0 3 * * *"])
    assert result.exit_code == 0
    assert "not a slot at this time of year" in result.output


def test_status_brief_is_one_line_and_succeeds() -> None:
    result = runner.invoke(app, ["status", "--brief"])
    assert result.exit_code == 0
    assert len(result.output.strip().splitlines()) == 1
    assert result.output.startswith("nhl-edge ")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "exactly one of --seasons, --start or --recent"),
        (["--seasons", "20232024", "--recent", "3"], "exactly one of"),
        (["--recent", "0"], "0 is not in the range x>=1"),
        (["--seasons", "2023"], "expected a season like 20232024"),
        (["--end", "2026-10-01", "--recent", "1"], "--end needs --start"),
        (["--start", "2026-10-02", "--end", "2026-10-01"], "--end is before --start"),
    ],
)
def test_ingest_rejects_bad_windows_before_any_call(args: list[str], message: str) -> None:
    result = runner.invoke(app, ["ingest", *args])
    assert result.exit_code == 2
    assert message in plain(result.output)


class SizedBucket:
    def __init__(self, sizes: list[int]) -> None:
        self.sizes = sizes

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        contents = [{"Key": f"raw/{i}", "Size": s} for i, s in enumerate(self.sizes)]
        return {"Contents": contents, "IsTruncated": False}


@pytest.mark.parametrize(("gb", "exit_code"), [(7.5, 0), (8.5, 1)])
def test_lake_size_fails_above_the_limit(
    monkeypatch: pytest.MonkeyPatch, gb: float, exit_code: int
) -> None:
    for name in R2_ENV:
        monkeypatch.setenv(name, "test")
    bucket = SizedBucket([int(gb * 2**30) - 1000, 1000])
    monkeypatch.setattr(R2Config, "client", lambda self: bucket)
    result = runner.invoke(app, ["lake", "size", "--max-gb", "8"])
    assert result.exit_code == exit_code
    assert f"R2 bucket test: 2 objects, {gb:.3f} GB (limit 8 GB)" in result.output
    assert ("above the 8 GB limit" in result.output) is (exit_code == 1)


def test_audit_shifts_needs_coverage_in_the_lake(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["audit", "shifts"])
    assert result.exit_code == 1
    assert "no shift coverage in the lake" in result.output


def test_audit_shifts_prints_one_row_per_season(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from feed_fixtures import MTL_ARI, OPENING_WEEK_GAMES, parsed_feeds

    from nhl_edge.lake.tables import Lake

    monkeypatch.chdir(tmp_path)
    for game_id in (*OPENING_WEEK_GAMES, MTL_ARI):
        Lake().write("shift_coverage", parsed_feeds(game_id)["shift_coverage"])
    result = runner.invoke(app, ["audit", "shifts"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert [line.split(" | ")[0] for line in lines[2:]] == ["| 20102011", "| 20222023"]
    only_new = runner.invoke(app, ["audit", "shifts", "--seasons", "20222023"])
    assert only_new.output.splitlines()[2].startswith("| 20222023 | 1 | 1 (100.0%)")


def test_audit_shifts_leaves_out_the_test_season_unless_asked(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import polars as pl
    from feed_fixtures import MTL_ARI, parsed_feeds

    from nhl_edge.lake.tables import Lake

    monkeypatch.chdir(tmp_path)
    row = parsed_feeds(MTL_ARI)["shift_coverage"]
    Lake().write("shift_coverage", row)
    test_season = row.with_columns(
        game_id=pl.lit(2025020060, pl.Int64), season=pl.lit(20252026, pl.Int32)
    )
    Lake().write("shift_coverage", test_season)
    default = runner.invoke(app, ["audit", "shifts"])
    assert [line.split(" | ")[0] for line in default.output.splitlines()[2:]] == ["| 20222023"]
    asked = runner.invoke(app, ["audit", "shifts", "--seasons", "20252026"])
    assert asked.output.splitlines()[2].startswith("| 20252026 | 1 |")


def test_audit_reference_needs_games_in_the_lake(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["audit", "reference"])
    assert result.exit_code == 1
    assert "no games in the lake" in result.output


def test_audit_reference_checks_every_game(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import polars as pl
    from test_reference import OPENING

    from nhl_edge.lake.tables import Lake

    monkeypatch.chdir(tmp_path)
    Lake().write("games", OPENING)
    clean = runner.invoke(app, ["audit", "reference"])
    assert clean.exit_code == 0, clean.output
    assert "3 games, 20102011 to 20102011: 0 problems" in clean.output
    Lake().write("games", OPENING.with_columns(venue=pl.lit("Nowhere Arena")))
    broken = runner.invoke(app, ["audit", "reference"])
    assert broken.exit_code == 1
    assert "venue 'Nowhere Arena' (3 games, e.g. 2010020003) is not in venues.csv" in broken.output


def test_status_reports_an_empty_lake(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status", "--brief"])
    assert result.exit_code == 0
    assert "lake empty: run nhl ingest" in result.output


def test_status_counts_rows_and_the_newest_date(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date
    from pathlib import Path

    from nhl_edge.ingest.games import listed_games, parse_games, schedule_of
    from nhl_edge.lake.tables import Lake

    week = (Path(__file__).parent / "fixtures/nhl_api/schedule_2010-10-07.json").read_bytes()
    games = parse_games(listed_games(week, {date(2010, 10, 7), date(2010, 10, 8)}), "k")
    monkeypatch.chdir(tmp_path)
    Lake().write("games", games)
    Lake().write("schedule", schedule_of(games))
    brief = runner.invoke(app, ["status", "--brief"])
    assert "lake to 2010-10-08: games 3, schedule 3 · missing: players, shots" in brief.output

    for name in R2_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("nhl_edge.settings.load_env", lambda: None)
    full = runner.invoke(app, ["status"])
    assert full.exit_code == 0, full.output
    assert "games                  3 rows in 2 files, to 2010-10-08" in full.output
    assert "R2: not configured" in full.output


def test_status_shows_what_r2_has_that_this_machine_lacks(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date
    from pathlib import Path

    import polars as pl
    from fakes import MemoryBucket

    from nhl_edge.ingest.games import listed_games, parse_games
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.tables import Lake

    week = (Path(__file__).parent / "fixtures/nhl_api/schedule_2010-10-07.json").read_bytes()
    games = parse_games(listed_games(week, {date(2010, 10, 7), date(2010, 10, 8)}), "k")
    for name in R2_ENV:
        monkeypatch.setenv(name, "test")
    monkeypatch.setattr("nhl_edge.settings.load_env", lambda: None)
    bucket = MemoryBucket()
    monkeypatch.setattr(R2Config, "client", lambda self: bucket)
    # The nightly runner mirrored a later date and an odds snapshot this machine never saw.
    Lake(tmp_path / "runner", "test", bucket).write("games", games)
    RawStore(tmp_path / "runner", "test", bucket).put("odds", "2010-10-08/snap", b"[]", {})
    monkeypatch.chdir(tmp_path)
    Lake().write("games", games.filter(pl.col("game_date") == date(2010, 10, 7)))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "games: 1 files only in R2, 0 only here, 0 differ in size" in result.output
    assert "raw/odds/: newest here None, in R2 odds/2010-10-08/snap" in result.output
    assert (
        "this machine is behind R2: nhl lake restore-raw, then "
        "nhl ingest --start 2010-10-08 --end 2010-10-08 --replay"
    ) in result.output
    assert "R2 lacks" not in result.output


def test_status_shows_pregame_polls_missing_here(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakes import MemoryBucket

    from nhl_edge.lake.raw import RawStore

    for name in R2_ENV:
        monkeypatch.setenv(name, "test")
    monkeypatch.setattr("nhl_edge.settings.load_env", lambda: None)
    bucket = MemoryBucket()
    monkeypatch.setattr(R2Config, "client", lambda self: bucket)
    # A runner polled a game's landing page, which only R2 holds: it cannot be fetched again.
    # Both sides hold an earlier poll of a later game, whose key sorts after the missing one.
    key = "pregame-landing/2026-09-29/2026020001/20260929T224500Z"
    shared = "pregame-landing/2026-09-29/2026020005/20260929T110500Z"
    RawStore(tmp_path / "runner", "test", bucket).put("nhl", key, b"{}", {})
    RawStore(tmp_path / "runner", "test", bucket).put("nhl", shared, b"{}", {})
    monkeypatch.chdir(tmp_path)
    RawStore().put("nhl", shared, b"{}", {})
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "raw/nhl/pregame-landing/: 1 responses only in R2, 0 only here" in result.output
    assert "pre-game goalie polls are behind R2: nhl lake restore-raw copies them" in result.output
    assert "up to date with R2" not in result.output


def test_status_notices_a_file_that_differs_from_r2(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, date, datetime

    import polars as pl
    from fakes import MemoryBucket

    from nhl_edge.lake.schemas import Players, dtypes
    from nhl_edge.lake.tables import Lake

    for name in R2_ENV:
        monkeypatch.setenv(name, "test")
    monkeypatch.setattr("nhl_edge.settings.load_env", lambda: None)
    bucket = MemoryBucket()
    monkeypatch.setattr(R2Config, "client", lambda self: bucket)
    monkeypatch.chdir(tmp_path)
    player = {
        "player_id": 8478402,
        "name": "Connor McDavid",
        "birth_date": date(1997, 1, 13),
        "shoots": "L",
        "draft_year": 2015,
        "draft_overall": 1,
        "fetched_utc": datetime(2026, 9, 28, tzinfo=UTC),
        "raw_key": "k",
    }
    # The same players file on both sides, but R2 holds an older, smaller copy.
    Lake().write("players", pl.DataFrame([player], schema=dtypes(Players)))
    bucket.objects["lake/players/part-0.parquet"] = b"older"
    result = runner.invoke(app, ["status"])
    assert "players: 0 files only in R2, 0 only here, 1 differ in size" in result.output
    assert "1 files differ from R2 in players: check which copy is current" in result.output
    # Neither side is assumed newer, so no command that would overwrite one is suggested.
    assert "R2 lacks" not in result.output
    assert "behind R2" not in result.output
    assert pl.read_parquet(tmp_path / "data/lake/players/part-0.parquet").height == 1


def test_status_brief_names_tables_that_lag(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date
    from pathlib import Path

    import polars as pl

    from nhl_edge.ingest.games import listed_games, parse_games, schedule_of
    from nhl_edge.lake.tables import Lake

    week = (Path(__file__).parent / "fixtures/nhl_api/schedule_2010-10-07.json").read_bytes()
    games = parse_games(listed_games(week, {date(2010, 10, 7), date(2010, 10, 8)}), "k")
    monkeypatch.chdir(tmp_path)
    Lake().write("games", games)  # an ingest with --no-feeds moved games on
    Lake().write("schedule", schedule_of(games.filter(pl.col("game_date") == date(2010, 10, 7))))
    brief = runner.invoke(app, ["status", "--brief"])
    assert (
        "lake to 2010-10-08: games 3, schedule 2 · behind: schedule to 2010-10-07" in brief.output
    )


@pytest.mark.parametrize("command", ["sync-raw", "restore-raw"])
def test_raw_sync_commands_need_r2(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    for name in R2_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("nhl_edge.settings.load_env", lambda: None)
    result = runner.invoke(app, ["lake", command])
    assert result.exit_code == 1


def test_restore_raw_reports_its_counts(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fakes import MemoryBucket

    from nhl_edge.lake.raw import RawStore

    for name in R2_ENV:
        monkeypatch.setenv(name, "test")
    bucket = MemoryBucket()
    RawStore(tmp_path / "runner", "test", bucket).put("odds", "2026-09-29/a", b"[]", {})
    monkeypatch.setattr(R2Config, "client", lambda self: bucket)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["lake", "restore-raw", "--prefix", "odds/"])
    assert result.exit_code == 0, result.output
    assert (
        "raw/odds/: 1 responses in R2, 0 here; downloaded 1, skipped 0 incomplete" in result.output
    )
    again = runner.invoke(app, ["lake", "sync-raw", "--prefix", "odds/"])
    assert (
        "raw/odds/: 1 responses here, 1 in R2; uploaded 0, skipped 0 incomplete here"
        in again.output
    )


def test_odds_replay_reports_its_matches(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    from nhl_edge.lake.raw import RawStore

    fixtures = Path(__file__).parent / "fixtures"
    monkeypatch.chdir(tmp_path)
    store = RawStore()
    odds = (fixtures / "odds_api/odds_eu_full_20260928T120053Z.json").read_bytes()
    meta = {"fetched_utc": "2026-09-28T12:00:53+00:00", "slot": "morning"}
    store.put("odds", "2026-09-28/s", odds, meta)
    week = (fixtures / "nhl_api/schedule_2026-09-28.json").read_bytes()
    store.put("nhl", "schedule/2026-09-28/20260928T120000Z", week, {"fetched_utc": "x"})
    result = runner.invoke(app, ["odds", "replay"])
    assert result.exit_code == 0, result.output
    assert "odds replay 2026-09-28..2026-09-28: 1 snapshots" in result.output
    assert "2 events; matched: regular season 2; unmatched 0" in result.output
    assert runner.invoke(app, ["odds", "replay", "--end", "2026-09-28"]).exit_code == 2


def test_status_sends_odds_drift_to_the_odds_replay(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, date, datetime

    import polars as pl
    from fakes import MemoryBucket

    from nhl_edge.lake.schemas import LakeOddsSnapshots, dtypes
    from nhl_edge.lake.tables import Lake

    for name in R2_ENV:
        monkeypatch.setenv(name, "test")
    monkeypatch.setattr("nhl_edge.settings.load_env", lambda: None)
    bucket = MemoryBucket()
    monkeypatch.setattr(R2Config, "client", lambda self: bucket)
    snapshot = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    row = {
        "snapshot_utc": snapshot,
        "last_update_utc": snapshot,
        "event_id": "e1",
        "commence_time_utc": datetime(2026, 9, 29, 23, 0, tzinfo=UTC),
        "home": "TOR",
        "away": "MTL",
        "book": "pinnacle",
        "market": "h2h",
        "side": "home",
        "line": None,
        "price_decimal": 1.8,
        "is_closing_proxy": False,
        "slot": "morning",
        "raw_key": "k",
        "snapshot_date": date(2026, 9, 28),
        "game_id": 2026020002,
        "game_type": 2,
    }
    frame = pl.DataFrame([row], schema=dtypes(LakeOddsSnapshots))
    Lake(tmp_path / "runner", "test", bucket).write("odds_snapshots", frame)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status"])
    assert "odds_snapshots is behind R2: nhl odds replay --r2" in result.output
    assert "nhl ingest" not in result.output.split("Against R2:")[1]


def test_status_sends_fitted_table_drift_to_its_own_command(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, date, datetime

    import polars as pl
    from fakes import MemoryBucket

    from nhl_edge.lake.schemas import ShotXg, dtypes
    from nhl_edge.lake.tables import Lake

    for name in R2_ENV:
        monkeypatch.setenv(name, "test")
    monkeypatch.setattr("nhl_edge.settings.load_env", lambda: None)
    bucket = MemoryBucket()
    monkeypatch.setattr(R2Config, "client", lambda self: bucket)
    row = {
        "game_id": 2026020001,
        "season": 20262027,
        "game_date": date(2026, 9, 29),
        "event_id": 7,
        "xg": 0.1,
        "train_cutoff": datetime(2026, 4, 17, 10, tzinfo=UTC),
        "artifact_version": "xg-20261002-a9b8ac4",
        "observed_utc": datetime(2026, 9, 30, 10, tzinfo=UTC),
    }
    frame = pl.DataFrame([row], schema=dtypes(ShotXg))
    Lake(tmp_path / "runner", "test", bucket).write("shot_xg", frame)
    monkeypatch.chdir(tmp_path)
    behind = plain(runner.invoke(app, ["status"]).output).split("Against R2:")[1]
    assert "shot_xg is behind R2: with the tables it reads up to date, nhl xg rebuilds it" in behind
    assert "nhl ingest" not in behind
    # This machine ahead of R2: the command with --r2 writes the rows.
    bucket.objects.clear()
    Lake(tmp_path / "data" / "lake").write("shot_xg", frame)
    ahead = plain(runner.invoke(app, ["status"]).output).split("Against R2:")[1]
    assert "R2 lacks shot_xg rows here: nhl xg --r2 from a clean main checkout" in ahead
    assert "nhl ingest" not in ahead


def test_status_sends_sbr_drift_to_the_sbr_import(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, date, datetime

    import polars as pl
    from fakes import MemoryBucket

    from nhl_edge.lake.schemas import SbrOdds, dtypes
    from nhl_edge.lake.tables import Lake

    for name in R2_ENV:
        monkeypatch.setenv(name, "test")
    monkeypatch.setattr("nhl_edge.settings.load_env", lambda: None)
    bucket = MemoryBucket()
    monkeypatch.setattr(R2Config, "client", lambda self: bucket)
    start = datetime(2018, 10, 3, 23, 0, tzinfo=UTC)
    row = {
        "game_id": 2018020001,
        "season": 20182019,
        "game_date": date(2018, 10, 3),
        "start_utc": start,
        "home": "TOR",
        "away": "MTL",
        "market": "h2h",
        "side": "home",
        "line": None,
        "quote": "close",
        "price_american": -150,
        "price_decimal": 1.0 + 100 / 150,
        "observed_utc": start,
        "assumed_available_utc": start,
        "raw_key": "sbr/20182019/x",
    }
    Lake(tmp_path / "runner", "test", bucket).write(
        "sbr_odds", pl.DataFrame([row], schema=dtypes(SbrOdds))
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status"])
    against = result.output.split("Against R2:")[1]
    assert (
        "sbr_odds is behind R2: nhl odds sbr --replay --r2 --seasons 20182019 restores" in against
    )
    assert "nhl ingest" not in against


def test_xg_scores_each_season_and_writes_the_report(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shot_fixtures import games_from, synthetic_shots

    from nhl_edge.ingest import games as games_module
    from nhl_edge.lake.tables import Lake

    shots = synthetic_shots([20102011, 20112012, 20122013], per_season=2000)
    games = games_from(shots)
    monkeypatch.setattr(
        games_module, "EXPECTED_GAMES", dict(games.group_by("season").len().iter_rows())
    )
    frames = {"shots": shots, "games": games}
    monkeypatch.setattr(Lake, "read", lambda self, table: frames[table])
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["xg", "--seasons", "20112012-20122013"])
    assert result.exit_code == 0, result.output
    assert "shots scored in 2 seasons" in result.output
    (report,) = (tmp_path / "reports" / "xg").glob("xg-*.md")
    assert "| 20112012 |" in report.read_text()
    assert list((tmp_path / "data" / "lake" / "shot_xg").rglob("*.parquet"))


def test_xg_refuses_a_lake_missing_games_or_the_play_before_each_shot(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import polars as pl
    from shot_fixtures import games_from, synthetic_shots

    from nhl_edge.ingest import games as games_module
    from nhl_edge.lake.tables import Lake

    shots = synthetic_shots([20102011, 20112012], per_season=500)
    games = games_from(shots)
    monkeypatch.setattr(games_module, "EXPECTED_GAMES", {20102011: games.height})
    stale = shots.with_columns(prev_event_type=pl.lit(None, dtype=pl.String))
    frames = {"shots": stale, "games": games}
    monkeypatch.setattr(Lake, "read", lambda self, table: frames[table])
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["xg", "--seasons", "20112012"])
    assert result.exit_code == 1
    output = plain(result.output)
    assert "20102011: " in output and "of " in output and "games" in output
    assert "games whose shots lack the play before them" in output
    assert "run nhl ingest --replay for those seasons" in output
    assert not (tmp_path / "data").exists()


def test_xg_refuses_a_season_with_nothing_before_it(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shot_fixtures import games_from, synthetic_shots

    from nhl_edge.ingest import games as games_module
    from nhl_edge.lake.tables import Lake

    shots = synthetic_shots([20102011], per_season=500)
    frames = {"shots": shots, "games": games_from(shots)}
    monkeypatch.setattr(games_module, "EXPECTED_GAMES", {})
    monkeypatch.setattr(Lake, "read", lambda self, table: frames[table])
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["xg", "--seasons", "20102011"])
    assert result.exit_code == 2
    assert "no earlier season" in plain(result.output)


def team_lake(monkeypatch: pytest.MonkeyPatch, drop_xg: bool = False) -> dict[str, Any]:
    import polars as pl
    from team_fixtures import league

    from nhl_edge.ingest import games as games_module
    from nhl_edge.lake.tables import Lake

    frames: dict[str, Any] = league()
    counts = dict(frames["games"].group_by("season").len().iter_rows())
    monkeypatch.setattr(games_module, "EXPECTED_GAMES", counts)
    if drop_xg:
        first = frames["games"]["game_id"][0]
        frames["shot_xg"] = frames["shot_xg"].filter(pl.col("game_id") != first)
    monkeypatch.setattr(Lake, "read", lambda self, table: frames[table])
    return frames


def test_team_strength_rates_every_game_with_the_frozen_settings(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames = team_lake(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["team-strength"])
    assert result.exit_code == 0, result.output
    games = frames["games"].height
    assert f"team_strength: {games:,} games rated with half-life 80, prior 40" in result.output
    assert list((tmp_path / "data" / "lake" / "team_strength").rglob("*.parquet"))


def goalie_lake(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from goalie_fixtures import league

    from nhl_edge.ingest import games as games_module
    from nhl_edge.lake.tables import Lake
    from nhl_edge.lineup import goalie_start as gs

    frames: dict[str, Any] = league()
    frames["actual_lineups"] = frames.pop("lineups")
    counts = dict(frames["games"].group_by("season").len().iter_rows())
    monkeypatch.setattr(games_module, "EXPECTED_GAMES", counts)
    monkeypatch.setattr(gs, "team_lines", lambda: {})
    monkeypatch.setattr(Lake, "read", lambda self, table: frames[table])
    return frames


def test_goalie_start_writes_the_table_and_the_report(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    goalie_lake(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["goalie-start"])
    assert result.exit_code == 0, result.output
    assert "candidate rows in 2 seasons" in result.output
    # The fixture's seasons are all training seasons, so every fit shows its size.
    assert "20122013: fitted on" in result.output and "held out" not in result.output
    assert list((tmp_path / "data" / "lake" / "goalie_starts").rglob("*.parquet"))
    (path,) = (tmp_path / "reports" / "goalie-start").glob("goalie-start-*.md")
    assert "## Per season" in path.read_text()


def test_goalie_start_prints_no_held_out_fits_size(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fit's team-games count only starters who were candidates: shown for a fit on a held-out
    # season, one fit's count less the previous one's would give that season's missed starters
    # (leakage check on #76).
    from nhl_edge.backtest import seasons

    goalie_lake(monkeypatch)
    monkeypatch.setattr(seasons, "OPEN_SEASONS", (20102011, 20122013))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["goalie-start"])
    assert result.exit_code == 0, result.output
    lines = {line.split(":")[0].strip(): line for line in result.output.splitlines()}
    assert "fitted on held out" in lines["20122013"]
    assert "team-games" in lines["20112012"]


def goalie_effect_lake(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import polars as pl
    from goalie_fixtures import with_shots

    from nhl_edge.features import team_strength as ts
    from nhl_edge.lineup import goalie_start as gs

    frames = goalie_lake(monkeypatch)
    monkeypatch.setattr(ts, "team_lines", lambda: {})
    league = with_shots({"games": frames["games"], "lineups": frames["actual_lineups"]})
    frames["shots"], frames["shot_xg"] = league["shots"], league["shot_xg"]
    frames["games"] = league["games"]
    frames["goalie_starts"], _, _ = gs.score(
        frames["actual_lineups"],
        frames["games"],
        [20112012, 20122013],
        "goalie-start-20261001-x",
        {},
    )
    # The fixture's 2010-11 has no xG, like the lake's.
    frames["games"] = frames["games"].filter(pl.col("season") >= 20112012)
    return frames


def test_goalie_effect_rates_every_candidate_with_the_frozen_settings(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge.features import goalie as ge

    frames = goalie_effect_lake(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["goalie-effect"])
    assert result.exit_code == 0, result.output
    rows = frames["goalie_starts"].height
    assert f"goalie_effects: {rows:,} candidates rated with {ge.TUNED.label}" in result.output
    assert list((tmp_path / "data" / "lake" / "goalie_effects").rglob("*.parquet"))


def test_goalie_effect_tune_logs_every_candidate(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge.features import goalie as ge

    goalie_effect_lake(monkeypatch)
    monkeypatch.setattr(ge, "TUNING_SEASONS", (20122013,))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["goalie-effect", "--tune"])
    assert result.exit_code == 0, result.output
    assert "chose half-life" in result.output and "the frozen TUNED" in result.output
    (report,) = (tmp_path / "reports" / "tuning").glob("goalie-effect-*.md")
    text = report.read_text()
    assert text.count("| half-life ") == 16 and "(chosen)" in text
    assert not (tmp_path / "data").exists()


def test_goalie_effect_refuses_a_game_without_goalie_starts(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import polars as pl

    frames = goalie_effect_lake(monkeypatch)
    first = frames["games"]["game_id"][0]
    frames["goalie_starts"] = frames["goalie_starts"].filter(pl.col("game_id") != first)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["goalie-effect"])
    assert result.exit_code == 1
    assert "1 games without goalie starts" in plain(result.output)


def schedule_lake(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from schedule_fixtures import league

    from nhl_edge.ingest import games as games_module
    from nhl_edge.lake.tables import Lake

    frames: dict[str, Any] = league()
    counts = dict(frames["games"].group_by("season").len().iter_rows())
    monkeypatch.setattr(games_module, "EXPECTED_GAMES", counts)
    monkeypatch.setattr(Lake, "read", lambda self, table: frames[table])
    return frames


def test_schedule_terms_rates_every_game_with_the_frozen_setting(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import polars as pl

    from nhl_edge.features import schedule_terms as st

    frames = schedule_lake(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["schedule-terms"])
    assert result.exit_code == 0, result.output
    rated = frames["schedule"].filter(pl.col("season") >= st.FIRST_SEASON).height
    assert f"schedule_terms: {rated:,} games rated with {st.TUNED.label}" in result.output
    assert list((tmp_path / "data" / "lake" / "schedule_terms").rglob("*.parquet"))


def test_schedule_terms_tune_logs_every_candidate(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge.features import schedule_terms as st

    schedule_lake(monkeypatch)
    monkeypatch.setattr(st, "TUNING_SEASONS", (20122013,))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["schedule-terms", "--tune"])
    assert result.exit_code == 0, result.output
    assert "chose prior" in result.output and "the frozen TUNED" in result.output
    (report,) = (tmp_path / "reports" / "tuning").glob("schedule-terms-*.md")
    text = report.read_text()
    assert text.count("| prior ") == 6 and "(chosen)" in text
    assert not (tmp_path / "data").exists()


def test_schedule_terms_refuses_a_game_without_an_arena(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import polars as pl

    frames = schedule_lake(monkeypatch)
    schedule = frames["schedule"]
    first = schedule.filter(pl.col("season") == 20112012)["game_id"][0]
    frames["schedule"] = schedule.with_columns(
        venue=pl.when(pl.col("game_id") == first)
        .then(pl.lit("Nowhere Arena"))
        .otherwise(pl.col("venue"))
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["schedule-terms"])
    assert result.exit_code == 1
    assert "1 games without an arena" in plain(result.output)


def test_goalie_start_refuses_a_lake_with_a_team_game_without_a_starter(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import polars as pl

    frames = goalie_lake(monkeypatch)
    lineups = frames["actual_lineups"]
    frames["actual_lineups"] = lineups.with_columns(
        starting_goalie=pl.when(pl.col("game_id") == lineups["game_id"][0])
        .then(False)
        .otherwise(pl.col("starting_goalie"))
    )
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["goalie-start"])
    assert result.exit_code == 1
    assert "2 team-games without a starter" in plain(result.output)
    assert not (tmp_path / "data").exists()


def test_goalie_start_refuses_a_season_with_nothing_earlier(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    goalie_lake(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["goalie-start", "--seasons", "20102011"])
    assert result.exit_code == 2
    assert "no earlier season" in plain(result.output)


def test_team_strength_tune_logs_every_candidate(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge.features import team_strength as ts

    team_lake(monkeypatch)
    monkeypatch.setattr(ts, "TUNING_SEASONS", (20122013,))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["team-strength", "--tune"])
    assert result.exit_code == 0, result.output
    assert "chose half-life" in result.output and "the frozen TUNED" in result.output
    (report,) = (tmp_path / "reports" / "tuning").glob("team-strength-*.md")
    text = report.read_text()
    assert text.count("| half-life ") == 16 and "(chosen)" in text
    assert not (tmp_path / "data").exists()


def test_team_strength_refuses_games_without_xg(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    team_lake(monkeypatch, drop_xg=True)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["team-strength"])
    assert result.exit_code == 1
    assert "1 games without xG" in plain(result.output)


def test_team_strength_refuses_a_season_without_xg(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    team_lake(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["team-strength", "--seasons", "20102011"])
    assert result.exit_code == 2
    assert "have no xG, so no team strength" in plain(result.output)
    assert not (tmp_path / "data").exists()


def stint_lake(monkeypatch: pytest.MonkeyPatch, frames: dict[str, Any]) -> None:
    import polars as pl

    from nhl_edge.lake.tables import Lake

    def read(self: Lake, table: str, seasons: Any = None) -> pl.DataFrame:
        frame = frames[table]
        return frame if seasons is None else frame.filter(pl.col("season").is_in(list(seasons)))

    monkeypatch.setattr(Lake, "read", read)


def test_stints_cuts_every_complete_game(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    import polars as pl
    from feed_fixtures import MTL_ARI, OPENING_WEEK_GAMES, parsed_feeds

    tables = [parsed_feeds(game_id) for game_id in (*OPENING_WEEK_GAMES, MTL_ARI)]
    frames = {name: pl.concat([t[name] for t in tables]) for name in tables[0]}
    from stint_fixtures import empty_shot_xg, real_calendar

    games = [*OPENING_WEEK_GAMES, MTL_ARI]
    frames["games"] = frames["shift_coverage"].join(
        real_calendar(games).with_columns(game_id=pl.Series(games)), on=["game_id", "season"]
    )
    frames["shot_xg"] = empty_shot_xg()
    stint_lake(monkeypatch, frames)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["stints", "--seasons", "20102011"])
    assert result.exit_code == 0, result.output
    assert "stints 20102011:" in result.output and "in 3 games, 0 left out of RAPM" in result.output
    assert list((tmp_path / "data" / "lake" / "stints").rglob("*.parquet"))
    # A season from 2011-12 on without any xG is refused: nhl xg runs first.
    refused = runner.invoke(app, ["stints", "--seasons", "20222023"])
    assert refused.exit_code == 1
    assert "20222023: no xG" in refused.output
    # A held-out season is written without showing its counts.
    from nhl_edge.features import xg

    scored = frames["shots"].filter(pl.col("season") == 20222023, xg.scorable())
    frames["shot_xg"] = scored.select(
        "game_id",
        "season",
        "event_id",
        xg=pl.lit(0.1),
        train_cutoff=pl.lit(datetime(2022, 9, 1, tzinfo=UTC)),
        artifact_version=pl.lit("xg-20261001-abc1234"),
    )
    held_out = runner.invoke(app, ["stints", "--seasons", "20222023"])
    assert held_out.exit_code == 0, held_out.output
    assert "stints 20222023: written" in held_out.output
    assert "left out" not in held_out.output
