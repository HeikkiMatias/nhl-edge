"""Growing the real bankroll (ADR 0047, #322): 12 weeks of real fills, read once.

The rule, accepted by the owner on 2026-10-29 before any real fill:
- the measure: net CLV per real fill at its matched cost, fees included, against the reference
  observed after it (live/fills.py's valued bids, #318), taker and maker fills apart. Its weekly
  block bootstrap interval must have its lower bound above 0 (hard rule 7);
- the sample: at least WEEKS Monday-to-Sunday weeks with real fills of the type, and the
  coverage floor: at least COVERAGE of its fills of started games have a reference observed
  after them;
- no stop has fired since the start (ADR 0039 item 5);
- it is read once, at the weekly report on the Monday WEEKS weeks after the Monday of the first
  real fill's week, on the fills and references public at that report's clock: the moment
  Sunday's results are public. The reading is taken as of that clock whenever it runs, written
  once, and repeated after; no earlier run states a verdict, and none later reads again.

A pass permits the owner to grow the real bankroll, up to the code's ceiling
(placement.CEILING_BANKROLL); it never grows it. The figures are Kalshi's fills, so they and the
verdict stay private (#280): `nhl real fills` writes them beside the fill report, and the weekly
report shows the counts and the reading date only.
"""

import json
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

from nhl_edge.ingest.games import result_public_utc
from nhl_edge.live import bundle as lb
from nhl_edge.live import fills as lf
from nhl_edge.live import placement
from nhl_edge.live import report as lr

WEEKS = 12
COVERAGE = 0.9
# The fill types read, each on its own sample; a fill whose type Kalshi didn't say is counted in
# neither.
KINDS = (lf.TAKER, lf.MAKER)
SHORT = f"fewer than {WEEKS} weeks of fills"
UNCOVERED = f"under {COVERAGE:.0%} of the fills with a reference after them"
NOT_ABOVE = "the CLV interval's lower bound is not above 0"
STOPPED = "a stop fired since the start"


def reading_date(found: pl.DataFrame) -> date | None:
    """The Monday WEEKS weeks after the Monday of the first real fill's week, or None before any
    fill."""
    if found.is_empty():
        return None
    first: date = found["game_date"].min()  # type: ignore[assignment]
    return first - timedelta(days=first.weekday()) + timedelta(weeks=WEEKS)


def clock(day: date) -> datetime:
    """The reading's clock: the weekly report's, once Sunday's results are public."""
    return result_public_utc(day - timedelta(days=1))


def samples(rows: pl.DataFrame) -> dict[str, dict[str, Any]]:
    """Each fill type's sample, counts only: its fills, the weeks they fall in, those of started
    games, those valued (a reference observed after them) and the share valued."""
    result = {}
    for kind in KINDS:
        of_kind = rows.filter(pl.col("kind") == kind)
        started = of_kind.filter(pl.col("reference") != lf.PENDING).height
        valued = of_kind.filter(pl.col("reference") == lf.VALUED).height
        result[kind] = {
            "fills": of_kind.height,
            "weeks": lr.weeks(of_kind),
            "started": started,
            "valued": valued,
            "coverage": valued / started if started else None,
        }
    return result


def figures(rows: pl.DataFrame) -> dict[str, dict[str, Any]]:
    """samples with each fill type's net CLV per valued bid, through the live report's estimate
    (its weekly block bootstrap interval, counts only under 4 weeks). Private (#280)."""
    valued = lf.bids(rows)
    found = samples(rows)
    for kind in KINDS:
        kept = valued.filter(pl.col("kind") == kind)
        found[kind]["clv_per_bid"] = lr.estimate(kept, "clv") if kept.height else None
    return found


def failing(sample: dict[str, Any], stopped: bool) -> list[str]:
    """What keeps one fill type's reading from passing, empty when it passes."""
    reasons = []
    if sample["weeks"] < WEEKS:
        reasons.append(SHORT)
    if sample["coverage"] is None or sample["coverage"] < COVERAGE:
        reasons.append(UNCOVERED)
    clv = sample["clv_per_bid"]
    if clv is None or "low" not in clv or not clv["low"] > 0:
        reasons.append(NOT_ABOVE)
    if stopped:
        reasons.append(STOPPED)
    return reasons


def take(
    found: pl.DataFrame,
    odds: pl.DataFrame,
    teams: pl.DataFrame,
    stopped_days: Sequence[date],
    day: date,
) -> dict[str, Any]:
    """The reading of day, as of its clock: only the fills observed before it, the references
    public by it, and the stops alerted before the day."""
    at = clock(day)
    before = found.filter(pl.col("fill_utc") < pl.lit(at, dtype=lf.FILL_SCHEMA["fill_utc"]))
    rows = lf.referenced(before, lf.references(odds, at), teams, at) if before.height else empty()
    stops = sorted(each for each in stopped_days if each < day)
    found_figures = figures(rows)
    result: dict[str, Any] = {
        "reading_date": day.isoformat(),
        "clock_utc": at.isoformat(),
        "stopped": [each.isoformat() for each in stops],
    }
    for kind in KINDS:
        reasons = failing(found_figures[kind], bool(stops))
        result[kind] = found_figures[kind] | {"passes": not reasons, "failing": reasons}
    return result


def empty() -> pl.DataFrame:
    """No referenced rows: before the first fill, or a reading before which nothing filled."""
    schema = lf.FILL_SCHEMA | {"reference": pl.String, "p_close": pl.Float64}
    return pl.DataFrame(schema=schema)


def reading_key(day: date) -> str:
    """Where the live exchange's reading is kept: beside its orders, once."""
    return f"{placement.PREFIXES[placement.LIVE]}/scale-up/{day.isoformat()}.json"


def private(
    store: lb.Store,
    found: pl.DataFrame,
    rows: pl.DataFrame,
    odds: pl.DataFrame,
    teams: pl.DataFrame,
    stopped_days: Sequence[date],
    now: datetime,
) -> dict[str, Any]:
    """The live exchange's scale-up block for the private fill report: before the reading's
    clock, the figures now as context, with no verdict; from it, the reading, taken once and
    written beside the orders, then read back. rows are found referenced now."""
    day = reading_date(found)
    if day is None:
        return {"reading_date": None}
    if now < clock(day):
        return {"reading_date": day.isoformat(), "read": False, "context": figures(rows)}
    key = reading_key(day)
    if key not in store.keys(f"{placement.PREFIXES[placement.LIVE]}/scale-up"):
        taken = take(found, odds, teams, stopped_days, day)
        lb.put_once(store, key, json.dumps(taken, sort_keys=True).encode())
    return {"reading_date": day.isoformat(), "read": True} | json.loads(store.get(key))


def public(rows: pl.DataFrame, found: pl.DataFrame, now: datetime) -> dict[str, Any]:
    """The weekly report's scale-up section: the reading date, whether its clock has passed, and
    each fill type's sample now, counts only. Never a CLV figure or the verdict (#280)."""
    day = reading_date(found)
    return {
        "reading_date": day.isoformat() if day is not None else None,
        "read": day is not None and now >= clock(day),
        "weeks_needed": WEEKS,
        "coverage_needed": COVERAGE,
        "samples": samples(rows),
    }


def count_line(block: dict[str, Any]) -> str:
    """The log's line for the private block: the reading date and weeks of fills, counts only."""
    if block["reading_date"] is None:
        return "real fills live: scale-up (ADR 0047) waits for a first fill"
    sample = block if block["read"] else block["context"]
    weeks = ", ".join(f"{kind} {sample[kind]['weeks']} weeks" for kind in KINDS)
    state = "read" if block["read"] else "reads"
    return f"real fills live: scale-up (ADR 0047) {state} on {block['reading_date']}; {weeks}"
