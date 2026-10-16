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
        "skipped": [],
        "drawdown_triggered": False,
        "as_of": IN_WINDOW,
        "venue": "pinnacle",
    } | changes
    return real.start_rule(**args)


def test_the_rule_holds_only_when_every_condition_does() -> None:
    assert rule()["verdict"] == "holds"
    assert rule(venue=None)["verdict"] == "fails: (d)"
    failing = rule(value=value(high=-0.001), paper_bankroll=99.0, venue=None)
    assert failing["verdict"] == "fails: (a), (b), (d)"


def test_each_condition_at_its_boundary() -> None:
    # (a) An upper bound of exactly 0 fails, of the CLV or of its pessimistic bound; so does a
    # coverage under the floor, and fewer than 4 weeks (counts only).
    assert rule(value=value(high=0.0))["failing"] == ["a"]
    assert rule(value=value(bound_high=0.0))["failing"] == ["a"]
    assert rule(value=value(share=0.899))["failing"] == ["a"]
    assert rule(value=value(share=0.90))["failing"] == []
    assert rule(value=value(high=None))["conditions"]["a"]["measured"] is False
    assert rule(value=value(high=None))["failing"] == ["a"]
    # (b) The owner's veto: exactly the start is not above it.
    assert rule(paper_bankroll=100.0)["failing"] == ["b"]
    assert rule(paper_bankroll=100.01)["failing"] == []
    # (c) Bundles unchecked or unfinished, a day skipped, or the drawdown review triggered.
    assert rule(unfinished=None)["failing"] == ["c"]
    assert rule(unfinished=[date(2026, 11, 3)])["failing"] == ["c"]
    assert rule(skipped=[date(2026, 11, 4)])["failing"] == ["c"]
    assert rule(drawdown_triggered=True)["failing"] == ["c"]
    assert rule(unfinished=None)["conditions"]["c"]["unfinished_bundles"] is None


def test_the_verdict_keeps_to_the_november_window() -> None:
    assert rule(as_of=real.START - timedelta(days=1))["verdict"] == "not before 2026-11-16"
    assert rule(as_of=real.START)["verdict"] == "holds"
    assert rule(as_of=real.END)["verdict"] == "holds"
    after = rule(as_of=real.END + timedelta(days=1))["verdict"]
    assert after == "the window has closed: a start needs a new decision by the owner"
    # The conditions are still shown outside the window.
    assert rule(as_of=real.START - timedelta(days=30))["failing"] == []


def test_the_weekly_clv_stop_applies_the_starts_safeguards() -> None:
    assert real.clv_stop(value(high=None)) == {"measured": False, "stop": False, "pause": False}
    assert real.clv_stop(value()) == {"measured": True, "stop": False, "pause": False}
    # Either interval entirely below 0 stops; a failed coverage floor pauses.
    assert real.clv_stop(value(high=-0.001))["stop"]
    assert real.clv_stop(value(bound_high=-0.001))["stop"]
    assert not real.clv_stop(value(high=0.0))["stop"]
    assert real.clv_stop(value(share=0.8)) == {"measured": True, "stop": False, "pause": True}


def test_adr_0039s_figures() -> None:
    assert (date(2026, 11, 16), date(2026, 11, 30)) == (real.START, real.END)
    assert (real.BANKROLL, real.STOP_DRAWDOWN) == (100.0, 0.20)
    # No venue until #266 confirms one.
    assert real.VENUE is None
