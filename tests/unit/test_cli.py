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


COMMANDS = ["ingest", "rate", "predict", "backtest", "bets", "odds", "lake", "audit", "status"]
STUBS = [
    ["rate"],
    ["predict"],
    ["backtest"],
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
