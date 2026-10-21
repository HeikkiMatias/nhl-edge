"""The Cloudflare timer in infra/timer and the workflows it starts (#50). Its own tests are in
infra/timer/test/ (node); these hold it to the Python side."""

import re
from pathlib import Path

from nhl_edge.ingest.odds import SLOT_PLANS

ROOT = Path(__file__).parents[2]
TIMER = (ROOT / "infra" / "timer" / "src" / "schedule.js").read_text()
WORKFLOWS = ROOT / ".github" / "workflows"
GUARD = "if: github.event_name != 'schedule' || vars.TIMER_ACTIVE != 'true'"


def test_timer_dispatches_every_odds_slot_at_its_eastern_time() -> None:
    block = re.search(r"ODDS_SLOTS = \{(.*?)\};", TIMER, re.S)
    assert block is not None
    timer = dict(re.findall(r'"(\d\d:\d\d)": "(\w+)"', block[1]))
    plan = {slot.et_time.strftime("%H:%M"): slot.name for slot in SLOT_PLANS["free-tier"]}
    assert timer == plan
    options = re.search(r"options: \[([^\]]*)\]", (WORKFLOWS / "odds-snapshots.yml").read_text())
    assert options is not None
    assert sorted(o.strip() for o in options[1].split(",")) == sorted(plan.values())


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
        == ["ingest-nightly.yml", "odds-snapshots.yml", "pregame-goalies.yml"]
    )
    for name in dispatched:
        text = (WORKFLOWS / name).read_text()
        assert "workflow_dispatch:" in text, name
        assert text.count("runs-on:") == text.count(GUARD) == 1, name


def test_a_workflow_left_to_github_s_schedule_never_skips_it() -> None:
    # Without the timer's dispatch, a guarded job would never run while TIMER_ACTIVE is true.
    for name in GITHUB_ONLY:
        text = (WORKFLOWS / name).read_text()
        assert "schedule:" in text and "workflow_dispatch:" in text, name
        assert "vars.TIMER_ACTIVE" not in text, name
