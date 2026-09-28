import pytest
from typer.testing import CliRunner

from nhl_edge.cli import app

runner = CliRunner()

COMMANDS = ["ingest", "rate", "predict", "backtest", "bets", "odds", "status"]
STUBS = [
    ["ingest"],
    ["rate"],
    ["predict"],
    ["backtest"],
    ["bets"],
    ["odds", "snapshot", "--regions", "eu", "--slot-plan", "free-tier", "--skip-if-no-games"],
    ["odds", "backfill"],
]


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


def test_status_brief_is_one_line_and_succeeds() -> None:
    result = runner.invoke(app, ["status", "--brief"])
    assert result.exit_code == 0
    assert len(result.output.strip().splitlines()) == 1
    assert result.output.startswith("nhl-edge ")
