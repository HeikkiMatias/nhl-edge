"""Real stakes beside the paper ledger (ADR 0039): the start rule and the stop rules, as the weekly
live report states them (#267).

Real stakes start on the first decision day from START to END on which the start rule held at
the latest weekly report; if it hasn't held by END, a start needs a new decision by the owner.
The rule, fixed before the evidence:
- **(a) CLV,** under ADR 0032's safeguards: v2's CLV per bet over at least 4 weeks has its
  interval's upper bound above 0, the coverage floor holds, and the pessimistic bound (each
  eligible bet without a proxy counted at the 10th percentile of the observed CLV) has its upper
  bound above 0 too.
- **(b) The paper bankroll** above its start: the owner's own veto, a point estimate that can
  only block a start, never pass one on its own (hard rule 7, the owner's decision).
- **(c) Clean operations:** every decision day's run bundle complete, no day skipped, and the
  drawdown review not triggered.
- **(d) A ready venue** (#266): VENUE, set by a commit once its terms are confirmed for a Finnish
  resident and placement works.

The weekly CLV stop applies the same safeguards: real bets stop when either interval falls
entirely below 0, and pause while the coverage floor fails. The 20% bankroll stop is checked
before every day's bets, from the real ledger (#268), not here.
"""

from collections.abc import Sequence
from datetime import date
from typing import Any

START = date(2026, 11, 16)
END = date(2026, 11, 30)
# The real bankroll in euros, and the fall from it that stops real bets.
BANKROLL = 100.0
STOP_DRAWDOWN = 0.20
# The pessimistic bound's percentile (live/report.BOUND's lower one).
PESSIMISTIC = "p10"
# The venue real bets are placed at, once ready (#266). None until then.
VENUE: str | None = None


def _clv(value: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, float | None]:
    """closing_value()'s CLV per bet, its pessimistic bound and the share of eligible bets with a
    proxy."""
    bound = value["bound"].get(PESSIMISTIC)
    return (
        value["clv_per_bet"],
        bound["clv_per_bet"] if bound else None,
        value["floor"]["share_with_proxy"],
    )


def start_rule(
    value: dict[str, Any],
    paper_bankroll: float,
    paper_start: float,
    unfinished: Sequence[date] | None,
    skipped: Sequence[date],
    drawdown_triggered: bool,
    as_of: date,
    venue: str | None,
) -> dict[str, Any]:
    """The start rule as of as_of: each condition with its figures and whether it holds, and the
    verdict. value is closing_value()'s result for the frozen policy, unfinished the decision days
    whose bundle has no manifest (None when not checked) and skipped the days skipped."""
    per_bet, bound, share = _clv(value)
    floor = value["floor"]["required"]
    measured = "high" in per_bet and bound is not None and "high" in bound
    conditions = {
        "a": {
            "clv_per_bet": per_bet,
            "bound": bound,
            "share_with_proxy": share,
            "floor": floor,
            "measured": measured,
            "holds": bool(
                measured
                and per_bet["high"] > 0
                and bound is not None
                and bound["high"] > 0
                and share is not None
                and share >= floor
            ),
        },
        "b": {
            "paper_bankroll": paper_bankroll,
            "start": paper_start,
            "holds": paper_bankroll > paper_start,
        },
        "c": {
            "unfinished_bundles": None
            if unfinished is None
            else [day.isoformat() for day in unfinished],
            "skipped_days": [day.isoformat() for day in skipped],
            "drawdown_triggered": drawdown_triggered,
            "holds": unfinished is not None
            and not unfinished
            and not skipped
            and not drawdown_triggered,
        },
        "d": {"venue": venue, "holds": venue is not None},
    }
    failing = [name for name, condition in conditions.items() if not condition["holds"]]
    if as_of < START:
        verdict = f"not before {START.isoformat()}"
    elif as_of > END:
        verdict = "the window has closed: a start needs a new decision by the owner"
    elif failing:
        verdict = "fails: " + ", ".join(f"({name})" for name in failing)
    else:
        verdict = "holds"
    return {
        "window": [START.isoformat(), END.isoformat()],
        "conditions": conditions,
        "failing": failing,
        "verdict": verdict,
    }


def clv_stop(value: dict[str, Any]) -> dict[str, Any]:
    """The weekly CLV stop under the start's safeguards: stop when the CLV per bet interval or the
    pessimistic bound's falls entirely below 0, pause while the coverage floor fails. Not yet
    measured under 4 weeks of bets."""
    per_bet, bound, share = _clv(value)
    if "high" not in per_bet or bound is None or "high" not in bound:
        return {"measured": False, "stop": False, "pause": False}
    return {
        "measured": True,
        "stop": per_bet["high"] < 0 or bound["high"] < 0,
        "pause": share is None or share < value["floor"]["required"],
    }
