"""ADR 0039's start rule and weekly CLV stop (live/real.py, #267): each condition at its boundary,
and the verdict before, in and after the November window."""

from datetime import date, timedelta
from typing import Any

from nhl_edge.live import real

IN_WINDOW = date(2026, 11, 16)


def value(
    high: float | None = 0.01, bound_high: float | None = 0.005, share: float | None = 1.0
) -> dict[str, Any]:
    """closing_value()'s result with the CLV per bet's and the pessimistic bound's upper bounds
    (counts only when None) and the share of eligible bets with a proxy."""

    def figure(upper: float | None) -> dict[str, Any]:
        if upper is None:
            return {"games": 40, "weeks": 3}
        return {"mean": upper - 0.02, "low": upper - 0.04, "high": upper, "games": 120, "weeks": 5}

    return {
        "clv_per_bet": figure(high),
        "floor": {"required": 0.90, "share_with_proxy": share},
        "bound": {"p10": {"clv_per_bet": figure(bound_high)}, "p90": {"clv_per_bet": figure(0.1)}},
    }


def rule(**changes: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "value": value(),
        "paper_bankroll": 104.49,
        "paper_start": 100.0,
        "unfinished": [],
        "undecided": [],
        "skipped": {},
        "drawdown_triggered": False,
        "as_of": IN_WINDOW,
        "venue": "pinnacle",
    } | changes
    return real.start_rule(**args)


def test_the_rule_holds_only_when_every_condition_does() -> None:
    assert rule()["verdict"] == "holds"
    assert rule(venue=None)["verdict"] == "fails: (d)"
    failing = rule(value=value(high=-0.001), venue=None)
    assert failing["verdict"] == "fails: (a), (d)"


def test_the_bankroll_is_the_owners_veto_never_a_pass() -> None:
    # A point estimate (hard rule 7): it applies or is lifted beside the rule, never among its
    # conditions (Codex on #273).
    vetoed = rule(paper_bankroll=100.0)
    assert vetoed["veto"]["applies"] and vetoed["failing"] == []
    assert vetoed["verdict"] == "the rule holds, but the owner's veto applies"
    assert not rule(paper_bankroll=100.01)["veto"]["applies"]
    both = rule(paper_bankroll=98.0, venue=None)["verdict"]
    assert both == "fails: (d); the owner's veto applies"
    assert "b" not in rule()["conditions"]


def test_the_drawdown_review_blocks_a_start_beside_the_rule() -> None:
    # Like the veto, a point estimate: it blocks, never fails a condition (Codex on #273).
    reviewed = rule(drawdown_triggered=True)
    assert reviewed["failing"] == [] and reviewed["drawdown_review"]["applies"]
    assert reviewed["verdict"] == "the rule holds, but the drawdown review is open"
    both = rule(drawdown_triggered=True, paper_bankroll=90.0)["verdict"]
    assert both == "the rule holds, but the owner's veto applies and the drawdown review is open"


def test_each_condition_at_its_boundary() -> None:
    # (a) An upper bound of exactly 0 fails, of the CLV or of its pessimistic bound; so does a
    # coverage under the floor, and fewer than 4 weeks (counts only).
    assert rule(value=value(high=0.0))["failing"] == ["a"]
    assert rule(value=value(bound_high=0.0))["failing"] == ["a"]
    assert rule(value=value(share=0.899))["failing"] == ["a"]
    assert rule(value=value(share=0.90))["failing"] == []
    assert rule(value=value(high=None))["conditions"]["a"]["measured"] is False
    assert rule(value=value(high=None))["failing"] == ["a"]
    # (c) Bundles unchecked or unfinished, a day with games and no decision, or the drawdown
    # review triggered. A day skipped with a recorded reason is listed, and doesn't fail (Codex
    # on #273).
    assert rule(unfinished=None)["failing"] == ["c"]
    assert rule(unfinished=[date(2026, 11, 3)])["failing"] == ["c"]
    assert rule(undecided=[date(2026, 11, 4)])["failing"] == ["c"]
    late = {date(2026, 11, 5): "day skipped: the decision came after the window"}
    explained = rule(skipped=late)
    assert explained["failing"] == []
    assert explained["conditions"]["c"]["skipped_days"] == {
        "2026-11-05": "day skipped: the decision came after the window"
    }
    assert rule(unfinished=None)["conditions"]["c"]["unfinished_bundles"] is None


def test_only_a_weekly_report_decides_and_only_in_november() -> None:
    assert rule(as_of=real.START - timedelta(days=1))["verdict"] == "not before 2026-11-16"
    assert rule(as_of=real.START)["verdict"] == "holds"
    assert rule(as_of=real.END)["verdict"] == "holds"
    # A nightly report between the weekly ones decides nothing (Codex on #273).
    tuesday = real.START + timedelta(days=1)
    assert not rule(as_of=tuesday)["weekly"]
    assert rule(as_of=tuesday)["verdict"] == (
        "decided at the weekly report, the next on 2026-11-23"
    )
    # A Monday report before Sunday's results are public decides nothing either (Codex on #273).
    early_monday = rule(complete=False)
    assert early_monday["verdict"] == (
        "decided at the weekly report, once the previous day's results are public"
    )
    after = rule(as_of=real.END + timedelta(days=1))["verdict"]
    assert after == "the window has closed: a start needs a new decision by the owner"
    # The conditions are still shown outside the window.
    assert rule(as_of=real.START - timedelta(days=30))["failing"] == []
    assert real.next_weekly(date(2026, 11, 22)) == date(2026, 11, 23)
    assert real.next_weekly(date(2026, 11, 23)) == date(2026, 11, 30)


def test_the_weekly_clv_stop_applies_the_starts_safeguards() -> None:
    monday = IN_WINDOW
    assert real.clv_stop(value(high=None), monday)["measured"] is False
    assert real.clv_stop(value(), monday) == {
        "measured": True,
        "weekly": True,
        "stop": False,
        "pause": False,
    }
    # Either interval entirely below 0 stops; a failed coverage floor pauses.
    assert real.clv_stop(value(high=-0.001), monday)["stop"]
    assert real.clv_stop(value(bound_high=-0.001), monday)["stop"]
    assert not real.clv_stop(value(high=0.0), monday)["stop"]
    assert real.clv_stop(value(share=0.8), monday)["pause"]
    # Between weekly reports nothing is decided, nor before the previous day's results are public.
    assert real.clv_stop(value(high=-0.001), monday, complete=False)["stop"] is None
    tuesday = monday + timedelta(days=1)
    assert real.clv_stop(value(high=-0.001), tuesday) == {
        "measured": True,
        "weekly": False,
        "stop": None,
        "pause": None,
    }


def test_adr_0039s_figures() -> None:
    assert date(2026, 11, 16) == real.START
    assert date(2026, 11, 30) == real.END
    assert real.START.weekday() == real.END.weekday() == real.WEEKLY
    assert (real.BANKROLL, real.STOP_DRAWDOWN) == (100.0, 0.20)
    # No venue until #266 confirms one.
    assert real.VENUE is None
