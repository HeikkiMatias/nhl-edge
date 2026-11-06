"""The Cloudflare timer in infra/timer and the workflows it starts (#50). Its own tests are in
infra/timer/test/ (node); these hold it to the Python side."""

import re
from pathlib import Path

from nhl_edge.ingest.odds import SLOT_PLANS

ROOT = Path(__file__).parents[2]
TIMER = (ROOT / "infra" / "timer" / "src" / "schedule.js").read_text()
WORKFLOWS = ROOT / ".github" / "workflows"
# Every job of a timed workflow skips its late schedule, alone or after always() (#327).
GUARD = "github.event_name != 'schedule' || vars.TIMER_ACTIVE != 'true'"


def test_timer_dispatches_every_odds_slot_at_its_eastern_time() -> None:
    block = re.search(r"ODDS_SLOTS = \{(.*?)\};", TIMER, re.S)
    assert block is not None
    timer = dict(re.findall(r'"(\d\d:\d\d)": "(\w+)"', block[1]))
    plan = {slot.et_time.strftime("%H:%M"): slot.name for slot in SLOT_PLANS["free-tier"]}
    assert timer == plan
    options = re.search(r"options: \[([^\]]*)\]", (WORKFLOWS / "odds-snapshots.yml").read_text())
    assert options is not None
    assert sorted(o.strip() for o in options[1].split(",")) == sorted(plan.values())


def test_timer_starts_the_kalshi_watch_at_its_windows() -> None:
    # A run's watch ends ten minutes into the next window (ingest/kalshi_watch.watch_end, #402).
    from nhl_edge.ingest.kalshi_watch import WINDOWS_ET

    block = re.search(r"KALSHI_WATCH_ET = \[(.*?)\];", TIMER, re.S)
    assert block is not None
    timer = re.findall(r'"(\d\d:\d\d)"', block[1])
    assert timer == [start.strftime("%H:%M") for start in WINDOWS_ET]


def test_the_kalshi_watch_fallback_lines_are_the_ones_its_cli_knows() -> None:
    from nhl_edge.ingest.kalshi_watch import FALLBACK_CRONS

    text = (WORKFLOWS / "kalshi-watch.yml").read_text()
    assert re.findall(r'- cron: "([^"]+)"', text) == list(FALLBACK_CRONS)
    assert '--cron "$CRON"' in text


def _jobs(text: str) -> dict[str, str]:
    """A workflow's jobs, each its own block of lines."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([\w-]+):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_each_kalshi_watch_run_has_its_own_concurrency_group() -> None:
    # #402: the next window's run starts while the last one still polls; one group for every run
    # would hold it back until the last ends, which is the hand-over gap.
    text = (WORKFLOWS / "kalshi-watch.yml").read_text()
    assert re.search(r"concurrency:\n  group: kalshi-watch-\$\{\{ github.run_id \}\}\n", text)


def test_the_kalshi_watch_rebuilds_and_quotes_one_run_at_a_time() -> None:
    # #402: what must not overlap has one group across runs, never cancelling a running job: the
    # tables' rebuild, after the watch whatever its outcome, and the demo quoter.
    jobs = _jobs((WORKFLOWS / "kalshi-watch.yml").read_text())
    assert sorted(jobs) == ["live", "quote", "rebuild", "watch"]
    for job, group in (("rebuild", "kalshi-watch-rebuild"), ("quote", "kalshi-watch-quote")):
        assert re.search(
            rf"\n    concurrency:\n      group: {group}\n      cancel-in-progress: false\n",
            jobs[job],
        ), job
    for job in ("watch", "live"):
        assert "concurrency:" not in jobs[job], job
    watch, rebuild = jobs["watch"], jobs["rebuild"]
    assert '--no-rebuild --dates-out "$GITHUB_OUTPUT"' in watch
    assert "dates: ${{ steps.watch.outputs.dates }}" in watch
    assert "line_dates: ${{ steps.watch.outputs.line_dates }}" in watch
    assert "needs: watch" in rebuild and "!cancelled()" in rebuild
    assert 'nhl kalshi rebuild --r2 --dates "$DATES" --line-dates "$LINE_DATES"' in rebuild


# Scheduled workflows the timer leaves to GitHub's own schedule: once a year, hours of delay don't
# matter, and adding one to the timer would need the deployed Worker redeployed before it is due.
GITHUB_ONLY = ["player-seasons.yml"]


def test_every_scheduled_workflow_is_dispatched_by_the_timer_and_skips_its_schedule() -> None:
    # Once TIMER_ACTIVE is set, a late GitHub schedule must not run a job a second time.
    scheduled = sorted(p.name for p in WORKFLOWS.glob("*.yml") if "schedule:" in p.read_text())
    dispatched = sorted(set(re.findall(r'workflow: "([\w-]+\.yml)"', TIMER)))
    assert (
        sorted(set(scheduled) - set(GITHUB_ONLY))
        == dispatched
        == [
            "ingest-nightly.yml",
            "kalshi-revalue.yml",
            "kalshi-watch.yml",
            "odds-snapshots.yml",
            "pregame-goalies.yml",
        ]
    )
    for name in dispatched:
        text = (WORKFLOWS / name).read_text()
        assert "workflow_dispatch:" in text, name
        assert text.count("runs-on:") == text.count(GUARD) >= 1, name


def test_a_workflow_left_to_github_s_schedule_never_skips_it() -> None:
    # Without the timer's dispatch, a guarded job would never run while TIMER_ACTIVE is true.
    for name in GITHUB_ONLY:
        text = (WORKFLOWS / name).read_text()
        assert "schedule:" in text and "workflow_dispatch:" in text, name
        assert "vars.TIMER_ACTIVE" not in text, name
