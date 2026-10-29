"""The jobs that can create, amend or cancel a Kalshi order run on the Kalshi runner the
repository variable KALSHI_RUNNER names (#347): Kalshi refuses trading from GitHub's cloud
machines in some US states. The runner itself is set up by infra/runner/README.md."""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).parents[2] / ".github" / "workflows"
KALSHI_RUNNER = (
    "vars.KALSHI_RUNNER == 'self-hosted' && fromJSON('[\"self-hosted\", \"kalshi\"]')"
    " || 'ubuntu-latest'"
)
# The commands that can send Kalshi an order, an amend or a cancel.
TRADING = ("nhl real place", "nhl real orders", "nhl real revalue", "nhl kalshi check")
# The nightly's orders run only reads: every order expired 5 minutes before its game, hours before.
READS_ONLY = {("ingest-nightly.yml", "ingest")}


def jobs(name: str) -> dict[str, str]:
    """A workflow's jobs by id, each its block's text."""
    body = (WORKFLOWS / name).read_text().split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([\w-]+):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def runs_on(job: str) -> str:
    found = re.search(r"^    runs-on: (.+)$", job, re.M)
    assert found is not None
    return found[1]


def test_every_job_that_can_trade_runs_on_the_kalshi_runner() -> None:
    trading = {
        (path.name, job_id)
        for path in WORKFLOWS.glob("*.yml")
        for job_id, job in jobs(path.name).items()
        if any(command in job for command in TRADING)
    }
    assert trading - READS_ONLY == {
        ("kalshi-cancel.yml", "cancel"),
        ("kalshi-check.yml", "check"),
        ("kalshi-revalue.yml", "revalue"),
        ("odds-snapshots.yml", "orders"),
        ("odds-snapshots.yml", "revalue"),
    }
    for name, job_id in trading - READS_ONLY:
        assert KALSHI_RUNNER in runs_on(jobs(name)[job_id]), (name, job_id)


def test_no_other_job_runs_on_the_kalshi_runner() -> None:
    # Above all, no workflow a pull request starts: a fork's code never reaches the machine.
    for path in WORKFLOWS.glob("*.yml"):
        for job_id, job in jobs(path.name).items():
            if not any(command in job for command in TRADING) or (path.name, job_id) in READS_ONLY:
                assert "self-hosted" not in job and "KALSHI_RUNNER" not in job, (path.name, job_id)
        if "pull_request" in path.read_text():
            assert "self-hosted" not in path.read_text(), path.name


def test_the_kill_switch_runs_on_githubs_runner_when_the_owner_picks_it() -> None:
    # When the machine is down, the owner can still cancel from GitHub's runner (or Kalshi's site).
    text = (WORKFLOWS / "kalshi-cancel.yml").read_text()
    assert "options: [kalshi, github]" in text and "default: kalshi" in text
    assert runs_on(jobs("kalshi-cancel.yml")["cancel"]).startswith(
        "${{ inputs.runner != 'github' && " + KALSHI_RUNNER
    )


def test_a_job_waiting_for_the_kalshi_runner_never_holds_back_a_snapshot() -> None:
    # The odds group is the snapshot job's own, so an orders or re-valuation job queued for a
    # machine that is down leaves the next slot's snapshot free to run.
    text = (WORKFLOWS / "odds-snapshots.yml").read_text()
    assert not re.search(r"^concurrency:", text, re.M)
    odds = jobs("odds-snapshots.yml")
    assert list(odds) == ["snapshot", "orders", "revalue"]
    assert "\n    concurrency: odds\n" in odds["snapshot"]
    assert runs_on(odds["snapshot"]) == "ubuntu-latest"
    assert not any(command in odds["snapshot"] for command in TRADING)
    # The orders go after the snapshot job's alert, and the slot's re-valuation after the orders,
    # neither after a snapshot job a later run replaced.
    assert "\n    needs: snapshot\n" in odds["orders"] and "group: kalshi-place" in odds["orders"]
    assert "\n    needs: [snapshot, orders]\n" in odds["revalue"]
    for job_id in ("orders", "revalue"):
        assert "always() && needs.snapshot.result != 'cancelled'" in odds[job_id], job_id
