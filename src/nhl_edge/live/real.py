"""Real stakes beside the paper ledger (ADR 0039): the start rule and the stop rules, as the weekly
live report states them (#267).

Real stakes start on the first decision day from START to END on which the start rule held at
the latest weekly report, Monday's (WEEKLY); if it hasn't held by END, a start needs a new
decision by the owner. Only a weekly report decides: the nightly ones show the figures and
point to the next weekly report (Codex on #273). The rule, fixed before the evidence:
- **(a) CLV,** under ADR 0032's safeguards: v2's CLV per bet over at least 4 weeks has its
  interval's upper bound above 0, the coverage floor holds, and the pessimistic bound (each
  eligible bet without a proxy counted at the 10th percentile of the observed CLV) has its upper
  bound above 0 too.
- **(c) Clean operations:** every decision day's run bundle complete, no day with games and no
  decision (a day skipped without a reason; days skipped with a recorded reason are listed),
  and the drawdown review not triggered.
- **(d) A ready venue** (#266): VENUE, set by a commit once its terms are confirmed for a Finnish
  resident and placement works.
- **The owner's veto,** ADR 0039's (b): the paper bankroll at or below its start blocks a start.
  It is a point estimate, so it is no pass or fail of its own (hard rule 7): it applies or is
  lifted, beside the rule (the owner's decision, 2026-10-16).

The weekly CLV stop applies the same safeguards: real bets stop when either interval falls
entirely below 0, and pause while the coverage floor fails. The 20% bankroll stop is checked
before every day's bets, from the real ledger (#268), not here.
"""

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

START = date(2026, 11, 16)
END = date(2026, 11, 30)
# The weekly report's day: Monday.
WEEKLY = 0
# The real bankroll in euros, and the fall from it that stops real bets.
BANKROLL = 100.0
STOP_DRAWDOWN = 0.20
# The pessimistic bound's percentile (live/report.BOUND's lower one).
PESSIMISTIC = "p10"
# The venue real bets are placed at, once ready (#266). None until then.
VENUE: str | None = None


def weekly(day: date) -> bool:
    """Whether a report as of day is a weekly one, which alone decides."""
    return day.weekday() == WEEKLY


def next_weekly(day: date) -> date:
    """The first weekly report's date after day."""
    return day + timedelta(days=(WEEKLY - day.weekday() - 1) % 7 + 1)


def _clv(value: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, float | None]:
    """closing_value()'s CLV per bet, its pessimistic bound and the share of eligible bets with a
    proxy."""
    bound = value["bound"].get(PESSIMISTIC)
    per_bet = value["clv_per_bet"]
    return per_bet, bound["clv_per_bet"] if bound else None, value["floor"]["share_with_proxy"]


def start_rule(
    value: Mapping[str, Any],
    paper_bankroll: float,
    paper_start: float,
    unfinished: Sequence[date] | None,
    undecided: Sequence[date],
    skipped: Mapping[date, str],
    drawdown_triggered: bool,
    as_of: date,
    venue: str | None,
) -> dict[str, Any]:
    """The start rule as of as_of: each condition with its figures and whether it holds, the
    owner's veto, and the verdict. value is closing_value()'s result for the frozen policy;
    unfinished the decision days whose bundle has no manifest (None when not checked); undecided
    the days with games and no decision; skipped the days skipped, with their recorded reason."""
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
        "c": {
            "unfinished_bundles": None
            if unfinished is None
            else [day.isoformat() for day in unfinished],
            "undecided_days": [day.isoformat() for day in undecided],
            "skipped_days": {day.isoformat(): reason for day, reason in sorted(skipped.items())},
            "drawdown_triggered": drawdown_triggered,
            "holds": unfinished is not None
            and not unfinished
            and not undecided
            and not drawdown_triggered,
        },
        "d": {"venue": venue, "holds": venue is not None},
    }
    veto = {
        "paper_bankroll": paper_bankroll,
        "start": paper_start,
        "applies": paper_bankroll <= paper_start,
    }
    failing = [name for name, condition in conditions.items() if not condition["holds"]]
    if as_of < START:
        verdict = f"not before {START.isoformat()}"
    elif as_of > END:
        verdict = "the window has closed: a start needs a new decision by the owner"
    elif not weekly(as_of):
        verdict = f"decided at the weekly report, the next on {next_weekly(as_of).isoformat()}"
    elif failing:
        verdict = "fails: " + ", ".join(f"({name})" for name in failing)
        verdict += "; the owner's veto applies too" if veto["applies"] else ""
    elif veto["applies"]:
        verdict = "the rule holds, but the owner's veto applies"
    else:
        verdict = "holds"
    return {
        "window": [START.isoformat(), END.isoformat()],
        "weekly": weekly(as_of),
        "conditions": conditions,
        "veto": veto,
        "failing": failing,
        "verdict": verdict,
    }


def clv_stop(value: Mapping[str, Any], as_of: date) -> dict[str, Any]:
    """The weekly CLV stop under the start's safeguards: stop when the CLV per bet interval or the
    pessimistic bound's falls entirely below 0, pause while the coverage floor fails. Not yet
    measured under 4 weeks of bets, and decided only at a weekly report (stop and pause None
    otherwise)."""
    per_bet, bound, share = _clv(value)
    if "high" not in per_bet or bound is None or "high" not in bound:
        return {"measured": False, "weekly": weekly(as_of), "stop": None, "pause": None}
    if not weekly(as_of):
        return {"measured": True, "weekly": False, "stop": None, "pause": None}
    return {
        "measured": True,
        "weekly": True,
        "stop": per_bet["high"] < 0 or bound["high"] < 0,
        "pause": share is None or share < value["floor"]["required"],
    }
