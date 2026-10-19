"""Real stakes beside the paper ledger (ADR 0039): the start rule and the stop rules, as the weekly
live report states them (#267).

Real stakes start on the first decision day from START to END on which the start rule held at
the latest weekly report, Monday's (WEEKLY); if it hasn't held by END, a start needs a new
decision by the owner. Only a weekly report decides, and only once the previous day's results
are public, on evidence public at its clock: the nightly ones show the figures and point to the
next weekly report (Codex on #273). The deciding run is the scheduled weekly live report,
Mondays at 10:20 UTC, after Sunday's results are public at 10:00 UTC; the 09:00 UTC nightly
defers to it (docs/handover.md, the weekly live report). The rule, fixed before the evidence:
- **(a) CLV,** under ADR 0032's safeguards: v2's CLV per bet over at least 4 weeks has its
  interval's upper bound above 0, the coverage floor holds, and the pessimistic bound (each
  eligible bet without a proxy counted at the 10th percentile of the observed CLV) has its upper
  bound above 0 too.
- **(c) Clean operations:** every decision day's run bundle complete, and no day with games and no
  decision (a day skipped without a reason; days skipped with a recorded reason are listed).
- **(d) A ready venue** (#266): VENUE, set by a commit once its terms are confirmed for a Finnish
  resident and placement works.
- **Two signals block a start** beside the rule, each a point estimate and so no pass or fail of
  its own (hard rule 7, Codex on #273): ADR 0039's (b), the owner's veto, which applies while the
  paper bankroll is at or below its start (the owner's decision, 2026-10-16); and its (c)'s
  drawdown review, open once the paper bankroll has fallen 20% from a peak (ADR 0032).

The weekly CLV stop applies the same safeguards: real bets stop when either interval falls
entirely below 0, and pause while the coverage floor fails. The 20% bankroll stop is checked
before every day's bets, from the real ledger (#268), not here.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Exchange:
    """An exchange's order rules (ADR 0040): its environment ("demo" or "live", live/kalshi_trade),
    the owner's deposit rate in USD per euro, fixed for the season, the contract step, the taker
    fee rate, the precision the fee is rounded up to (a direct member's $0.0001), and how long
    before the listed start an order expires."""

    env: str
    usd_per_eur: float
    step: float = 0.01
    taker_fee: float = 0.07
    fee_precision: float = 0.0001
    expire_before: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if self.env not in ("demo", "live") or self.usd_per_eur <= 0:
            raise ValueError(f"an exchange needs its environment and rate: {self}")


@dataclass(frozen=True)
class Venue:
    """Where real bets are placed (#266): a stake is rounded down to increment euros, and one
    below min_stake is skipped, never rounded up (ADR 0039). An exchange venue buys contracts
    under its exchange's rules, and its bets are placed automatically (ADR 0040)."""

    name: str
    min_stake: float
    increment: float
    exchange: Exchange | None = None


# The practice alerts before the start (#268): any stake to the cent.
PRACTICE_VENUE = Venue("practice", 0.0, 0.01)
# Kalshi's demo exchange, mock funds, where the practice days' orders go (ADR 0040): its rate
# is a round stand-in, since no euro is converted there.
DEMO_EXCHANGE = Exchange("demo", usd_per_eur=1.15)
# The venues whose contract stays open past a postponement (ADR 0040, #287): Kalshi settles a
# game played within two weeks of its listed start on its result, and resolves a later or
# cancelled one at its own "last fair price", so such a bet is held until that is known.
EXCHANGE_VENUES = frozenset({"kalshi"})
EXCHANGE_POSTPONED = timedelta(days=14)
# Each venue's stake rules, entered with VENUE once its terms are confirmed (#266). Kalshi's
# entry, with the owner's deposit rate: Venue("kalshi", 0.0, 0.01, Exchange("live", <rate>)).
VENUES: dict[str, Venue] = {}
# The venue real bets are placed at, once ready (#266). None until then.
VENUE: str | None = None
# A stop holds until the owner restarts real bets, by setting this to the first day they may
# resume in a commit (ADR 0039): stops recorded before it no longer hold (#268).
RESTARTED_ON: date | None = None


def venue() -> Venue:
    """The ready venue's stake rules: real bets need both VENUE and its entry in VENUES."""
    if VENUE is None or VENUE not in VENUES:
        raise ValueError(f"no stake rules for the venue {VENUE!r}: set VENUE and VENUES (#266)")
    return VENUES[VENUE]


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
    complete: bool = True,
) -> dict[str, Any]:
    """The start rule as of as_of: each condition with its figures and whether it holds, the two
    signals that block a start, and the verdict. value is closing_value()'s result for the frozen
    policy on the evidence public at the report's clock; unfinished the decision days whose
    bundle has no manifest (None when not checked); undecided the days with games and no
    decision; skipped the days skipped, with their recorded reason; complete whether the
    previous day's results were public by the report's clock, which a weekly decision needs."""
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
            "holds": unfinished is not None and not unfinished and not undecided,
        },
        "d": {"venue": venue, "holds": venue is not None},
    }
    veto = {
        "paper_bankroll": paper_bankroll,
        "start": paper_start,
        "applies": paper_bankroll <= paper_start,
    }
    review = {"applies": drawdown_triggered}
    blocks = [
        text
        for text, signal in (
            ("the owner's veto applies", veto),
            ("the drawdown review is open", review),
        )
        if signal["applies"]
    ]
    failing = [name for name, condition in conditions.items() if not condition["holds"]]
    if as_of < START:
        verdict = f"not before {START.isoformat()}"
    elif as_of > END:
        verdict = "the window has closed: a start needs a new decision by the owner"
    elif not weekly(as_of):
        verdict = f"decided at the weekly report, the next on {next_weekly(as_of).isoformat()}"
    elif not complete:
        verdict = "decided at the weekly report, once the previous day's results are public"
    elif failing:
        # A condition without its interval yet is unmeasured, never a failure (hard rule 7, Codex
        # on #293): the rule doesn't hold, and the verdict says why.
        verdict = "does not hold: " + ", ".join(
            f"({name}) not yet measured"
            if not conditions[name].get("measured", True)
            else f"({name})"
            for name in failing
        )
        verdict += "".join(f"; {text}" for text in blocks)
    elif blocks:
        verdict = "the rule holds, but " + " and ".join(blocks)
    else:
        verdict = "holds"
    return {
        "window": [START.isoformat(), END.isoformat()],
        "weekly": weekly(as_of),
        "complete": complete,
        "conditions": conditions,
        "veto": veto,
        "drawdown_review": review,
        "failing": failing,
        "verdict": verdict,
    }


def clv_stop(value: Mapping[str, Any], as_of: date, complete: bool = True) -> dict[str, Any]:
    """The weekly CLV stop under the start's safeguards: stop when the CLV per bet interval or the
    pessimistic bound's falls entirely below 0, pause while the coverage floor fails. Not yet
    measured under 4 weeks of bets, and decided only at a weekly report once the previous day's
    results are public (stop and pause None otherwise)."""
    per_bet, bound, share = _clv(value)
    decides = weekly(as_of) and complete
    if "high" not in per_bet or bound is None or "high" not in bound:
        return {"measured": False, "weekly": decides, "stop": None, "pause": None}
    if not decides:
        return {"measured": True, "weekly": False, "stop": None, "pause": None}
    return {
        "measured": True,
        "weekly": True,
        "stop": per_bet["high"] < 0 or bound["high"] < 0,
        "pause": share is None or share < value["floor"]["required"],
    }
