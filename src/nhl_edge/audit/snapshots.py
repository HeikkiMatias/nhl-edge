"""Live snapshot health: whether each odds slot landed on the days it should have, how late, how
old its moneyline quotes were, how many events matched an NHL game, and the Odds API credits spent.

Every stored response under odds/ in the raw cache is one run of `nhl odds snapshot`, with its
slot, fetch time and credit counts in the sidecar. A slot was due on an ET day when the job would
have called the Odds API (slot_has_games) given the day's games, regular season and playoffs, as
the cached NHL schedule listings last show them (audit.games.listed_games). A game moved off the
day by a later listing counts on its new date only, so a slot due only for it counts as not due.

A run belongs to the ET day of its slot time, which is the ET day it was fetched on, or the day
before when the fetch came before that day's slot time: a run can start late but never early.
"""

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

import polars as pl

from nhl_edge.ingest.nhl_api import ScheduledGame, parse_utc
from nhl_edge.ingest.odds import ET, SLOT_PLANS, SOURCE, Slot, slot_has_games
from nhl_edge.ingest.odds_lake import dated_raw_keys, is_complete
from nhl_edge.lake.raw import RawStore

PLAN = "free-tier"
# A run this much after its slot time is listed as late. A reporting threshold only: a pre-game
# slot runs 15 minutes before the first start it serves.
LATE = timedelta(minutes=15)
RUNS_SCHEMA = {
    "raw_key": pl.String,
    "slot": pl.String,
    "fetched_utc": pl.Datetime("us", "UTC"),
    "slot_day": pl.Date,
    "delay_min": pl.Float64,
    "credits_last": pl.Int64,
    "credits_remaining": pl.Int64,
}


def _slot_at(slot: Slot, day: date) -> datetime:
    return datetime.combine(day, slot.et_time, tzinfo=ET).astimezone(UTC)


def slot_runs(store: RawStore, plan: str = PLAN) -> pl.DataFrame:
    """One row per stored odds response: its slot, fetch time, the ET day and slot time it ran
    for, how late it was, and its credit counts. Responses of slots outside the plan are left
    out."""
    slots = {slot.name: slot for slot in SLOT_PLANS[plan]}
    rows = []
    for keys in dated_raw_keys(SOURCE, store).values():
        for raw_key in keys:
            if not is_complete(store, raw_key):
                continue
            meta = store.meta(raw_key)
            slot = slots.get(str(meta.get("slot")))
            if slot is None:
                continue
            fetched = parse_utc(meta["fetched_utc"])
            day = fetched.astimezone(ET).date()
            if fetched < _slot_at(slot, day):
                day -= timedelta(days=1)
            credits = meta.get("credits") or {}
            rows.append(
                {
                    "raw_key": raw_key,
                    "slot": slot.name,
                    "fetched_utc": fetched,
                    "slot_day": day,
                    "delay_min": (fetched - _slot_at(slot, day)).total_seconds() / 60,
                    "credits_last": credits.get("last"),
                    "credits_remaining": credits.get("remaining"),
                }
            )
    return pl.DataFrame(rows, schema=RUNS_SCHEMA).sort("fetched_utc")


def due_slots(listed: pl.DataFrame, days: Iterable[date], plan: str = PLAN) -> pl.DataFrame:
    """For every ET day and slot of the plan, whether the slot was due: whether the job, run at
    the slot time, would have found a game to price among the listed games (game_id, game_type,
    start_utc, home, away), which slot_has_games narrows to regular season and playoffs."""
    columns = ("game_id", "game_type", "start_utc", "home", "away")
    games = [ScheduledGame(*row) for row in listed.select(columns).iter_rows()]
    rows = [
        {
            "slot_day": day,
            "slot": slot.name,
            "due": slot_has_games(slot, games, _slot_at(slot, day)),
        }
        for day in days
        for slot in SLOT_PLANS[plan]
    ]
    return pl.DataFrame(rows, schema={"slot_day": pl.Date, "slot": pl.String, "due": pl.Boolean})


def slot_report(runs: pl.DataFrame, due: pl.DataFrame, plan: str = PLAN) -> pl.DataFrame:
    """Per slot: days it was due, days it landed, days it was missed, days it ran though not due
    (manual runs, or a slot the schedule no longer shows games for), and its delays on the days
    it was due, from the first run of each."""
    per_day = runs.group_by("slot_day", "slot").agg(
        pl.len().alias("runs"), pl.col("delay_min").min().alias("delay_min")
    )
    days = due.join(per_day, on=["slot_day", "slot"], how="full", coalesce=True).with_columns(
        pl.col("due").fill_null(False), pl.col("runs").fill_null(0)
    )
    order = {slot.name: i for i, slot in enumerate(SLOT_PLANS[plan])}
    return (
        days.group_by("slot")
        .agg(
            pl.col("due").sum().alias("due"),
            (pl.col("due") & (pl.col("runs") > 0)).sum().alias("landed"),
            (pl.col("due") & (pl.col("runs") == 0)).sum().alias("missed"),
            (~pl.col("due") & (pl.col("runs") > 0)).sum().alias("not_due_runs"),
            pl.col("delay_min").filter("due").median().alias("delay_median_min"),
            pl.col("delay_min").filter("due").max().alias("delay_max_min"),
        )
        .sort(pl.col("slot").replace_strict(order, default=len(order)))
    )


def quote_age(odds: pl.DataFrame) -> pl.DataFrame:
    """Per slot, how old the moneyline (h2h) quotes were when stored, from the book's last_update:
    over every book and for Pinnacle, whose quotes make the closing proxy."""
    age = (pl.col("snapshot_utc") - pl.col("last_update_utc")).dt.total_seconds() / 60
    h2h = odds.filter(pl.col("market") == "h2h").with_columns(age_min=age)

    def summary(frame: pl.DataFrame, books: str) -> pl.DataFrame:
        return (
            frame.group_by("slot")
            .agg(
                pl.len().alias("quotes"),
                pl.col("age_min").median().alias("age_median_min"),
                pl.col("age_min").quantile(0.9).alias("age_p90_min"),
                pl.col("age_min").max().alias("age_max_min"),
                (pl.col("age_min") > 60).mean().alias("over_an_hour"),
            )
            .with_columns(books=pl.lit(books))
        )

    return pl.concat(
        [summary(h2h, "all"), summary(h2h.filter(pl.col("book") == "pinnacle"), "pinnacle")]
    ).sort("books", "slot")


def event_matches(odds: pl.DataFrame) -> pl.DataFrame:
    """The Odds API events priced, one row each, with the NHL game matched to it (null if none)."""
    return (
        odds.sort("snapshot_utc")
        .unique("event_id", keep="last")
        .select("event_id", "home", "away", "commence_time_utc", "game_id", "game_type")
        .sort("commence_time_utc", "event_id")
    )


def credit_report(runs: pl.DataFrame) -> pl.DataFrame:
    """Per ET day: Odds API calls stored, credits they cost, and credits left after the last."""
    return (
        runs.sort("fetched_utc")
        .group_by("slot_day", maintain_order=True)
        .agg(
            pl.len().alias("calls"),
            pl.col("credits_last").sum().alias("credits_spent"),
            pl.col("credits_remaining").last().alias("credits_left"),
        )
        .sort("slot_day")
    )


def problems(runs: pl.DataFrame, due: pl.DataFrame, events: pl.DataFrame, as_of: date) -> list[str]:
    """Missed slots, late runs of due slots, and events starting by as_of that no NHL game
    matched, one line each. A later event can still match once the nightly ingest caches the
    schedule week that lists it."""
    due_days = due.filter("due").select("slot_day", "slot")
    # A slot day is judged by its first run: a retry or manual rerun after it is not late.
    first = runs.sort("fetched_utc").unique(["slot_day", "slot"], keep="first")
    missed = due_days.join(first, on=["slot_day", "slot"], how="anti").sort("slot_day")
    late = (
        first.join(due_days, on=["slot_day", "slot"])
        .filter(pl.col("delay_min") > LATE.total_seconds() / 60)
        .sort("slot_day", "fetched_utc")
    )
    started = pl.col("commence_time_utc").dt.convert_time_zone(ET.key).dt.date() <= as_of
    lines = [
        f"{day} {slot}: due, no snapshot stored"
        for day, slot in missed.select("slot_day", "slot").iter_rows()
    ]
    lines += [
        f"{row['slot_day']} {row['slot']}: stored {row['delay_min']:.0f} minutes after its slot "
        f"time ({row['raw_key']})"
        for row in late.iter_rows(named=True)
    ]
    lines += [
        f"event {row['event_id']} ({row['away']} at {row['home']}, "
        f"{row['commence_time_utc']:%Y-%m-%d %H:%M} UTC) matches no NHL game"
        for row in events.filter(pl.col("game_id").is_null() & started).iter_rows(named=True)
    ]
    return lines


def _number(value: float | None, digits: int = 0) -> str:
    return "" if value is None else f"{value:,.{digits}f}"


def markdown_report(
    slots: pl.DataFrame, ages: pl.DataFrame, credits: pl.DataFrame, events: pl.DataFrame
) -> str:
    """The snapshot tables as markdown, for the audit report."""
    lines = [
        "| Slot | Due | Landed | Missed | Ran when not due | Median delay (min) "
        "| Max delay (min) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    lines += [
        f"| {r['slot']} | {r['due']} | {r['landed']} | {r['missed']} | {r['not_due_runs']} "
        f"| {_number(r['delay_median_min'])} | {_number(r['delay_max_min'])} |"
        for r in slots.iter_rows(named=True)
    ]
    lines += [
        "",
        "Age of h2h quotes when stored (snapshot time minus the book's last_update):",
        "",
        "| Books | Slot | Quotes | Median (min) | 90th percentile (min) | Max (min) "
        "| Over an hour |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    lines += [
        f"| {r['books']} | {r['slot']} | {r['quotes']:,} | {_number(r['age_median_min'], 1)} "
        f"| {_number(r['age_p90_min'], 1)} | {_number(r['age_max_min'], 1)} "
        f"| {r['over_an_hour']:.1%} |"
        for r in ages.iter_rows(named=True)
    ]
    matched = events.filter(pl.col("game_id").is_not_null()).height
    lines += [
        "",
        f"Events priced: {events.height}, matched to an NHL game: {matched}.",
        "",
        "| ET day | Calls | Credits spent | Credits left |",
        "| --- | ---: | ---: | ---: |",
    ]
    lines += [
        f"| {r['slot_day']} | {r['calls']} | {_number(r['credits_spent'])} "
        f"| {_number(r['credits_left'])} |"
        for r in credits.iter_rows(named=True)
    ]
    return "\n".join(lines)
