"""Real stakes day by day (#268, ADR 0039): the alert, the owner's entries and settlement.

- **The alert** (`nhl real alert`, after the 12:45 ET paper decision is written): each paper bet
  of the day, with the lowest price it may be placed at and its stake at each price, goes to the
  owner by Telegram. The day is written once to R2 (real/live/alerts/<date>.json) before the
  message is sent, and copied to Supabase for the dashboard's form. The paper ledger is only read.
- **The mode,** from committed evidence only: practice before the start; real from the first
  weekly report from START to END whose start rule holds (live/real.py, ADR 0039); paused while
  the latest weekly report's coverage floor fails; stopped once a weekly report's CLV stop says
  so, or the real bankroll is 20% below its start. Without a start by END, the window closes.
  Practice rows never count in the real bankroll.
- **The stake** at a price o is the frozen staking rule recomputed there (`staking.fractions`),
  the day's other bets held at their paper prices, so the day cap's scaling is the tightest it
  can be, and never more than the paper fraction: times the bankroll, rounded down to the venue's
  increment, and none below its minimum stake. The minimum price is the lowest price, to the
  cent, at which the expected return p·o - 1 still clears the bet's hurdle (hard rule 4).
- **The entries:** the owner records each bet on the dashboard (Supabase's real_entries, which
  refuses a second entry and a placed time at or after the start). Each is copied once to R2
  (real/live/entries/), the record; a mistake is corrected by an appended correction
  (real/live/corrections/), never an edit.
- **Settlement** on the full game, OT and shootout included (hard rule 2), at the matched price
  and the stake placed, appended once per placed bet (real/live/settlements/); a correction to a
  settled bet appends its next revision. The bankroll at a decision counts only results public
  before it.
"""

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from nhl_edge.backtest.walk_forward import outcomes
from nhl_edge.betting import staking
from nhl_edge.betting.selection import POLICY, POLICY_VERSION
from nhl_edge.lake.schemas import REAL_MODES, RealAlerts, RealBets, RealSettlements, dtypes
from nhl_edge.live import bundle as lb
from nhl_edge.live import real
from nhl_edge.live.settle import POSTPONED

PREFIX = "real/live"
# The committed weekly reports (nhl live report), which alone decide the start and the stops.
REPORTS = Path("reports/live")
HELSINKI = ZoneInfo("Europe/Helsinki")
CENT = 100

PRACTICE, REAL, PAUSED, STOPPED, CLOSED = REAL_MODES
ALERTED = "alerted"
SKIP_STARTED = "skipped: the game started before the alert"
SKIP_MINIMUM = "skipped: the stake is below the venue's minimum"
SKIP_MODE = {
    PAUSED: "skipped: real bets are paused",
    STOPPED: "skipped: real bets are stopped",
    CLOSED: "skipped: the start window closed without a start",
}
PLACED, NOT_PLACED, SKIPPED = "placed", "not placed", "skipped"
SETTLED, VOID, CANCELLED = "settled", "void", "cancelled"
# An entry as the dashboard's form records it (Supabase real_entries) and R2 keeps it.
ENTRY_FIELDS = (
    "game_date",
    "game_id",
    "outcome",
    "matched_price",
    "stake",
    "bet_id",
    "placed_utc",
    "reason",
    "entered_utc",
    "late",
)
# What a correction may change.
CORRECTABLE = ("outcome", "matched_price", "stake", "bet_id", "placed_utc", "reason")


@dataclass(frozen=True)
class Mode:
    """The day's mode, the weekly report the start came from, and why, in plain words."""

    name: str
    started: date | None = None
    reason: str = ""


def weekly_reports(directory: Path = REPORTS) -> list[dict[str, Any]]:
    """The frozen policy's committed weekly reports (report-<date>.json of a Monday), oldest
    first: the only ones that decide the start rule and the weekly CLV stop."""
    found = []
    for path in sorted(directory.glob("report-*.json")):
        if not re.fullmatch(r"report-\d{4}-\d{2}-\d{2}\.json", path.name):
            continue
        data = json.loads(path.read_text())
        section = data.get("real_stakes")
        if data.get("policy_version") == POLICY_VERSION and section and section.get("weekly"):
            found.append(data)
    return sorted(found, key=lambda data: data["as_of"])


def _as_of(report: Mapping[str, Any]) -> date:
    return date.fromisoformat(report["as_of"])


def mode(
    reports: Sequence[Mapping[str, Any]],
    day: date,
    real_bankroll: float,
    stopped: Sequence[date] = (),
) -> Mode:
    """The day's mode from the weekly reports dated on or before it, the real bankroll at its
    decision, and the earlier days alerted as stopped: a stop holds until the owner restarts
    (real.RESTARTED_ON), whatever the bankroll does after it (Codex on #276)."""
    seen = [report for report in reports if _as_of(report) <= day]
    start = next(
        (
            _as_of(report)
            for report in seen
            if real.START <= _as_of(report) <= real.END
            and report["real_stakes"]["verdict"] == "holds"
        ),
        None,
    )
    if start is None:
        if day > real.END:
            return Mode(
                CLOSED,
                reason=f"the start rule didn't hold by {real.END:%d %b}, so a start needs your "
                "decision",
            )
        return Mode(PRACTICE)
    since = [report for report in seen if _as_of(report) >= start]
    held = [
        each
        for each in stopped
        if start <= each < day and (real.RESTARTED_ON is None or each >= real.RESTARTED_ON)
    ]
    if held:
        return Mode(STOPPED, start, f"real bets were stopped on {min(held):%d %b}")
    for report in since:
        if report["real_stakes"]["clv_stop"].get("stop"):
            return Mode(
                STOPPED,
                start,
                f"the weekly report of {report['as_of']} found v2's closing value, or its "
                "pessimistic bound, entirely below 0",
            )
    floor = real.BANKROLL * (1 - real.STOP_DRAWDOWN)
    if real_bankroll <= floor + 1e-9:
        return Mode(
            STOPPED,
            start,
            f"the real bankroll is {real_bankroll:.2f} €, at or below {floor:.2f} €",
        )
    if since[-1]["real_stakes"]["clv_stop"].get("pause"):
        return Mode(
            PAUSED,
            start,
            f"the weekly report of {since[-1]['as_of']} found fewer than 90% of the eligible "
            "bets with a closing price",
        )
    return Mode(REAL, start)


def min_price(p_side: float, hurdle: float, paper_price: float) -> float:
    """The lowest price, to the cent, at which the expected return p·o - 1 clears the hurdle,
    never above the paper price, where it did."""
    cents = math.ceil(round((1 + hurdle) / p_side * CENT, 6))
    return min(cents / CENT, paper_price)


def round_down(amount: float, venue: real.Venue) -> float:
    """A stake rounded down to the venue's increment, or 0 below its minimum (ADR 0039)."""
    stake = round(math.floor(round(amount / venue.increment, 6)) * venue.increment, 2)
    return stake if stake > 0 and stake >= venue.min_stake else 0.0


def stake_tables(
    bets: pl.DataFrame, bankroll: float, venue: real.Venue
) -> dict[int, list[tuple[float, float]]]:
    """Per paper bet of a day (game_id), its stake in euros at each price to the cent from its
    minimum price up to its paper price, which ends the list; above it the stake stays the paper
    price's. Only prices with a stake the venue takes are listed, so an empty list is no bet."""
    day = bets.select("season", "game_date", "game_id", "price", "p_side", "u_sd", "fraction")
    tables = {}
    for row in bets.iter_rows(named=True):
        low = min_price(row["p_side"], row["hurdle"], row["price"])
        top = math.ceil(round(row["price"] * CENT, 6))
        prices = [cents / CENT for cents in range(round(low * CENT), top)] + [row["price"]]
        table = []
        for price in prices:
            trial = day.with_columns(
                price=pl.when(pl.col("game_id") == row["game_id"])
                .then(pl.lit(price))
                .otherwise(pl.col("price"))
            ).with_columns(ev=pl.col("p_side") * pl.col("price") - 1)
            recomputed = (
                staking.fractions(trial.drop("fraction"), POLICY)
                .filter(pl.col("game_id") == row["game_id"])["fraction"]
                .item()
            )
            stake = round_down(min(recomputed, row["fraction"]) * bankroll, venue)
            if stake > 0:
                table.append((price, stake))
        tables[row["game_id"]] = table
    return tables


def stake_at(table: Sequence[Sequence[float]], price: float) -> float:
    """The stake a table allows at a price: its row at the highest listed price at or below it,
    or 0 below the first."""
    allowed = 0.0
    for listed, stake in table:
        if listed <= price + 1e-9:
            allowed = stake
    return allowed


def alert_rows(
    ledger: pl.DataFrame,
    today: Mode,
    bankroll: float,
    venue: real.Venue,
    now: datetime,
    code_version: str,
) -> pl.DataFrame:
    """One row per paper bet of the day's ledger: alerted with its minimum price and stakes, or
    why it is skipped."""
    columns = dtypes(RealAlerts)
    bets = ledger.filter(pl.col("bet").fill_null(False)).sort("game_id")
    if bets.is_empty():
        return pl.DataFrame(schema=columns)
    tables = stake_tables(bets, bankroll, venue)
    rows = []
    for row in bets.iter_rows(named=True):
        table = tables[row["game_id"]]
        if today.name in SKIP_MODE:
            status = SKIP_MODE[today.name]
        elif row["start_utc"] <= now:
            status = SKIP_STARTED
        elif not table:
            status = SKIP_MINIMUM
        else:
            status = ALERTED
        rows.append(
            {
                **{name: row[name] for name in ("game_date", "season", "game_id", "start_utc")},
                **{name: row[name] for name in ("home", "away", "policy_version")},
                "prediction_utc": row["prediction_utc"],
                "alert_utc": now,
                "mode": today.name,
                "venue": venue.name,
                "status": status,
                "side": row["side"],
                "paper_price": row["price"],
                "p_side": row["p_side"],
                "hurdle": row["hurdle"],
                "paper_fraction": row["fraction"],
                "min_price": table[0][0]
                if table
                else min_price(row["p_side"], row["hurdle"], row["price"]),
                "bankroll": bankroll,
                "max_stake": table[-1][1] if table else 0.0,
                "stakes": json.dumps([list(pair) for pair in table]),
                "code_version": code_version,
            }
        )
    return RealAlerts.validate(pl.DataFrame(rows, schema=columns))  # type: ignore[return-value]


def _price(price: float) -> str:
    """A decimal price with two or three decimals: 2.15, 1.917."""
    text = f"{price:.3f}"
    return text[:-1] if text.endswith("0") else text


def ladder(table: Sequence[Sequence[float]], rows: int = 4) -> list[tuple[float, float]]:
    """At most rows steps of a stake table for the message, highest price first: each stake with
    the lowest price that gives it, the full stake and the lowest always among them."""
    levels: dict[float, float] = {}
    for price, stake in table:
        levels.setdefault(stake, price)
    steps = sorted(((price, stake) for stake, price in levels.items()), reverse=True)
    if len(steps) <= rows:
        return steps
    picks = sorted({round(i * (len(steps) - 1) / (rows - 1)) for i in range(rows)})
    return [steps[i] for i in picks]


def _day(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def message(
    day: date,
    today: Mode,
    rows: pl.DataFrame,
    bankroll: float,
    url: str | None,
    skipped_day: str | None = None,
) -> str:
    """The day's Telegram message, in plain words: each alerted bet's game, side, minimum price
    and stakes, or why there is none."""
    if today.name == CLOSED:
        return (
            f"NHL Edge · {_day(day)}\nReal stakes didn't start: {today.reason}. "
            "The practice alerts end here."
        )
    practice = today.name == PRACTICE
    header = f"PRACTICE, don't bet · {_day(day)}" if practice else f"real bets · {_day(day)}"
    lines = [
        f"NHL Edge · {header}",
        f"{'Practice' if practice else 'Real'} bankroll {bankroll:.2f} €",
        "",
    ]
    if today.name == STOPPED:
        return "\n".join([*lines, f"Real bets are stopped: {today.reason}.", _restart()])
    if today.name == PAUSED:
        lines.append(
            f"Real bets are paused: {today.reason}. They resume once a weekly report finds the "
            "floor holds again."
        )
        return "\n".join(lines)
    if skipped_day is not None:
        lines.append(f"The paper decision skipped today ({skipped_day}).")
    alerted = rows.filter(pl.col("status") == ALERTED)
    for row in rows.iter_rows(named=True):
        team = row["home"] if row["side"] == "home" else row["away"]
        start = row["start_utc"].astimezone(HELSINKI)
        game = f"{row['away']} at {row['home']}, starts {start:%a %H:%M}"
        if row["status"] != ALERTED:
            lines.append(f"{game}: no bet ({row['status'].removeprefix('skipped: ')})")
            continue
        steps = ladder(json.loads(row["stakes"]))
        stakes = " · ".join(
            f"{stake:.2f} € at {_price(price)}{' or more' if i == 0 else ''}"
            for i, (price, stake) in enumerate(steps)
        )
        lines += [
            f"{game} · bet {team}",
            f"  minimum price {_price(row['min_price'])}",
            f"  stake: {stakes}",
        ]
    if alerted.is_empty():
        lines.append(f"No {'practice' if practice else 'real'} bets today.")
    else:
        lines += [
            "",
            "Between two prices, use the lower one's stake. Below the minimum price, don't bet.",
        ]
        if url:
            lines.append(f"Record each bet on the dashboard: {url}")
    if practice:
        lines.append("Practice only: nothing here is a real bet.")
    return "\n".join(lines)


def _restart() -> str:
    return "Restarting needs your decision. The paper ledger goes on."


@dataclass(frozen=True)
class Alert:
    """A day's alert: its mode, the bankroll and venue it staked, its rows and its message."""

    day: date
    today: Mode
    bankroll: float
    venue: real.Venue
    rows: pl.DataFrame
    message: str
    alert_utc: datetime
    code_version: str

    def record(self) -> dict[str, Any]:
        """The day's write-once record in R2."""
        return {
            "game_date": self.day.isoformat(),
            "mode": self.today.name,
            "started": self.today.started.isoformat() if self.today.started else None,
            "reason": self.today.reason,
            "bankroll": self.bankroll,
            "venue": self.venue.name,
            "alert_utc": self.alert_utc.isoformat(),
            "code_version": self.code_version,
            "message": self.message,
            "rows": jsonable(self.rows),
        }


def prepare(
    day: date,
    ledger: pl.DataFrame,
    reports: Sequence[Mapping[str, Any]],
    bets: pl.DataFrame,
    games: pl.DataFrame,
    now: datetime,
    code_version: str,
    url: str | None = None,
    stopped: Sequence[date] = (),
) -> Alert:
    """The day's alert from its written paper ledger, the committed weekly reports, the real
    record so far (bets), the games' results and the earlier days alerted as stopped, at the
    clock now."""
    decision = ledger["prediction_utc"][0]
    real_bankroll = bankroll(bets, games, decision, REAL)
    today = mode(reports, day, real_bankroll, stopped)
    if today.name in (PRACTICE, CLOSED):
        venue, staked = real.PRACTICE_VENUE, bankroll(bets, games, decision, PRACTICE)
    else:
        venue, staked = real.venue(), real_bankroll
    rows = alert_rows(ledger, today, staked, venue, now, code_version)
    skipped = ledger.filter(pl.col("status").str.starts_with("day skipped"))
    reason = (
        skipped["status"][0].removeprefix("day skipped: ")
        if skipped.height == ledger.height
        else None
    )
    text = message(day, today, rows, staked, url, reason)
    return Alert(day, today, staked, venue, rows, text, now, code_version)


def publishable(
    build: Callable[[datetime], Alert], clock: Callable[[], datetime], tries: int = 5
) -> Alert:
    """The alert built at a clock reading, then checked against the clock again just before it is
    written and sent: while an alerted game has started by then, the day is built again at the
    later reading, so a game under way is never alerted (Codex on #276)."""
    now = clock()
    for _ in range(tries):
        alert = build(now)
        later = clock()
        started = alert.rows.filter(pl.col("status") == ALERTED, pl.col("start_utc") <= later)
        if started.is_empty():
            return alert
        now = later
    raise ValueError(f"games kept starting while the alert was built ({tries} tries)")


def stored_rows(record: Mapping[str, Any]) -> pl.DataFrame:
    """A stored alert record's rows, as written."""
    rows = frame(record["rows"], dtypes(RealAlerts))
    return RealAlerts.validate(rows)  # type: ignore[return-value]


def jsonable(frame: pl.DataFrame) -> list[dict[str, Any]]:
    """Rows as JSON-ready dicts, every instant and date in ISO form, exactly."""
    out = []
    for row in frame.to_dicts():
        out.append(
            {
                name: value.isoformat() if isinstance(value, datetime | date) else value
                for name, value in row.items()
            }
        )
    return out


def frame(rows: Sequence[Mapping[str, Any]], columns: Mapping[str, pl.DataType]) -> pl.DataFrame:
    """jsonable's rows back as a frame of the columns."""
    parsed = []
    for row in rows:
        out = {}
        for name, dtype in columns.items():
            value = row.get(name)
            if isinstance(value, str) and isinstance(dtype, pl.Datetime):
                stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                value = stamp.astimezone(UTC)
            elif isinstance(value, str) and dtype == pl.Date:
                value = date.fromisoformat(value[:10])
            out[name] = value
        parsed.append(out)
    return pl.DataFrame(parsed, schema=dict(columns))


def alerts_key(day: date) -> str:
    return f"{PREFIX}/alerts/{day.isoformat()}.json"


def entry_key(game_date: date, game_id: int) -> str:
    return f"{PREFIX}/entries/{game_date.isoformat()}/{game_id}.json"


def correction_key(game_date: date, game_id: int, n: int) -> str:
    return f"{PREFIX}/corrections/{game_date.isoformat()}/{game_id}-{n}.json"


def settlement_key(game_date: date, game_id: int, revision: int) -> str:
    return f"{PREFIX}/settlements/{game_date.isoformat()}/{game_id}-{revision}.parquet"


def alerted(store: lb.Store, day: date) -> dict[str, Any] | None:
    """The day's alert record, or None before it is written."""
    key = alerts_key(day)
    if key not in store.keys(f"{PREFIX}/alerts"):
        return None
    return json.loads(store.get(key))


def write_alert(store: lb.Store, alert: Alert) -> str:
    """Write the day's alert record once: a second run is refused, never written over."""
    key = alerts_key(alert.day)
    if key in store.keys(f"{PREFIX}/alerts"):
        raise ValueError(f"{alert.day} is already alerted ({key}); --resend sends it again")
    store.put(key, json.dumps(alert.record(), sort_keys=True).encode())
    return key


def stopped_days(store: lb.Store) -> list[date]:
    """The days alerted as stopped, from their records."""
    days = []
    for key in store.keys(f"{PREFIX}/alerts"):
        if json.loads(store.get(key))["mode"] == STOPPED:
            days.append(date.fromisoformat(Path(key).stem))
    return sorted(days)


def resend_message(record: Mapping[str, Any], now: datetime, url: str | None) -> str:
    """A stored alert's message to send again at now: as stored while none of its alerted games
    has started, and otherwise written again from its rows with those games as no bet, so a game
    under way is never sent as a bet (Codex on #276). The record itself stays as written."""
    rows = stored_rows(record)
    started = (pl.col("status") == ALERTED) & (pl.col("start_utc") <= now)
    if rows.filter(started).is_empty():
        return record["message"]
    rows = rows.with_columns(
        status=pl.when(started).then(pl.lit(SKIP_STARTED)).otherwise(pl.col("status"))
    )
    started_on = date.fromisoformat(record["started"]) if record.get("started") else None
    today = Mode(record["mode"], started_on, record.get("reason", ""))
    day = date.fromisoformat(record["game_date"])
    text = message(day, today, rows, record["bankroll"], url)
    return f"{text}\n(Sent again at {now.astimezone(HELSINKI):%H:%M}.)"


def closed_already(store: lb.Store) -> bool:
    """Whether a day after the window was already alerted: the closed window's message goes once."""
    return any(
        date.fromisoformat(Path(key).stem) > real.END for key in store.keys(f"{PREFIX}/alerts")
    )


def alerts(store: lb.Store) -> pl.DataFrame:
    """Every alerted day's rows (the lake's real_alerts)."""
    columns = dtypes(RealAlerts)
    rows = [
        row for key in store.keys(f"{PREFIX}/alerts") for row in json.loads(store.get(key))["rows"]
    ]
    built = frame(rows, columns)
    return RealAlerts.validate(built.sort("game_date", "game_id"))  # type: ignore[return-value]


def supabase_alerts(rows: pl.DataFrame) -> list[dict[str, Any]]:
    """The alert rows as the dashboard's real_alerts holds them: the stakes as a list."""
    keep = (
        "game_date",
        "game_id",
        "season",
        "start_utc",
        "home",
        "away",
        "policy_version",
        "mode",
        "venue",
        "status",
        "side",
        "paper_price",
        "min_price",
        "max_stake",
        "bankroll",
        "alert_utc",
    )
    out = []
    for row in jsonable(rows):
        record = {name: row[name] for name in keep}
        record["stakes"] = [
            {"price": price, "stake": stake} for price, stake in json.loads(row["stakes"])
        ]
        out.append(record)
    return out


# The settlement's columns the dashboard's real_settlements holds (#270): all of them.
SETTLEMENT_KEY = ("game_date", "game_id", "revision")


def supabase_settlements(settled: pl.DataFrame) -> list[dict[str, Any]]:
    """The settlement revisions as the dashboard's real_settlements holds them."""
    return jsonable(settled.select(list(dtypes(RealSettlements))))


CORRECTION_KEY = ("game_date", "game_id", "n")


def days(store: lb.Store) -> list[dict[str, Any]]:
    """Each alerted day's record, without its message and rows: its mode, start, reason,
    bankroll, venue and alert time, and how many paper bets it had (#270)."""
    keep = ("game_date", "mode", "started", "reason", "bankroll", "venue", "alert_utc")
    out = []
    for key in store.keys(f"{PREFIX}/alerts"):
        record = json.loads(store.get(key))
        out.append({name: record.get(name) for name in keep} | {"bets": len(record["rows"])})
    return out


def supabase_days(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The days as the dashboard's real_days holds them: one row a day, a day with no bets too,
    so a start, pause, stop or close on such a day is still on record there (Codex on #278)."""
    return [dict(record) | {"reason": record.get("reason") or ""} for record in records]


def supabase_corrections(corrected: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The corrections as the dashboard's real_corrections holds them (#270)."""
    keep = ("game_date", "game_id", "n", "changes", "reason", "corrected_utc")
    return [{name: correction[name] for name in keep} for correction in corrected]


def store_entries(store: lb.Store, entries: Sequence[Mapping[str, Any]]) -> int:
    """Copy the dashboard's entries to R2, each once: one already there with the same bytes
    counts as copied, with other bytes it is refused (an entry is never edited). Returns how many
    were new."""
    held = set(store.keys(f"{PREFIX}/entries"))
    new = 0
    for entry in entries:
        body = json.dumps({name: entry.get(name) for name in ENTRY_FIELDS}, sort_keys=True)
        key = entry_key(date.fromisoformat(str(entry["game_date"])), int(entry["game_id"]))
        new += key not in held
        lb.put_once(store, key, body.encode())
    return new


def entries(store: lb.Store) -> list[dict[str, Any]]:
    return [json.loads(store.get(key)) for key in store.keys(f"{PREFIX}/entries")]


def corrections(store: lb.Store) -> list[dict[str, Any]]:
    found = [json.loads(store.get(key)) for key in store.keys(f"{PREFIX}/corrections")]
    return sorted(found, key=lambda c: (c["corrected_utc"], c["game_date"], c["game_id"], c["n"]))


def correct(
    store: lb.Store,
    game_date: date,
    game_id: int,
    changes: Mapping[str, Any],
    reason: str,
    now: datetime,
) -> str:
    """Append a correction to a bet's entry, with its reason: the next one of that bet. One that
    would leave the record invalid, such as a placed bet without its bet id, is refused."""
    wrong = sorted(set(changes) - set(CORRECTABLE))
    if wrong or not changes:
        raise ValueError(f"a correction changes some of {CORRECTABLE}, not {wrong or 'nothing'}")
    # The venue's precision, as the entries and the dashboard's tables hold it: a finer value
    # would settle differently there than in R2 (Codex on #278).
    for name, places in (("matched_price", 3), ("stake", 2)):
        value = changes.get(name)
        if value is not None and abs(round(float(value), places) - float(value)) > 1e-9:
            raise ValueError(f"{name} has more than {places} decimals: {value}")
    if not reason.strip():
        raise ValueError("a correction needs its reason")
    prefix = f"{PREFIX}/corrections/{game_date.isoformat()}/{game_id}-"
    n = sum(key.startswith(prefix) for key in store.keys(f"{PREFIX}/corrections"))
    key = correction_key(game_date, game_id, n)
    record = {
        "game_date": game_date.isoformat(),
        "game_id": game_id,
        "n": n,
        "changes": dict(changes),
        "reason": reason,
        "corrected_utc": now.isoformat(),
    }
    bets(alerts(store), entries(store), [*corrections(store), record])
    store.put(key, json.dumps(record, sort_keys=True).encode())
    return key


def _instant(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def bets(
    alerted_rows: pl.DataFrame,
    recorded: Sequence[Mapping[str, Any]],
    corrected: Sequence[Mapping[str, Any]] = (),
) -> pl.DataFrame:
    """Every alert row with what the owner did (the lake's real_bets): its entry, with the
    corrections applied in order, or "not placed" when an alerted bet has none."""
    found: dict[tuple[str, int], dict[str, Any]] = {}
    for entry in recorded:
        found[(str(entry["game_date"])[:10], int(entry["game_id"]))] = dict(entry)
    counts: dict[tuple[str, int], int] = {}
    for correction in corrected:
        key = (correction["game_date"], int(correction["game_id"]))
        entry = found.setdefault(
            key,
            {"entered_utc": correction["corrected_utc"], "late": None, "reason": None},
        )
        entry.update(correction["changes"])
        counts[key] = counts.get(key, 0) + 1
    keys = {(row["game_date"].isoformat(), row["game_id"]) for row in alerted_rows.to_dicts()}
    orphans = sorted(set(found) - keys)
    if orphans:
        raise ValueError(f"entries without an alert: {orphans[:3]}")
    rows = []
    for alert in alerted_rows.sort("game_date", "game_id").iter_rows(named=True):
        key = (alert["game_date"].isoformat(), alert["game_id"])
        entry = found.get(key, {})
        if alert["status"] != ALERTED:
            if entry:
                raise ValueError(f"an entry for a bet that wasn't alerted: {key}")
            outcome = SKIPPED
        else:
            outcome = entry.get("outcome") or NOT_PLACED
        placed = outcome == PLACED
        needed = ("matched_price", "stake", "bet_id", "placed_utc")
        if placed and any(entry.get(name) is None for name in needed):
            raise ValueError(f"a placed bet needs its {', '.join(needed)}: {key}")
        when = _instant(entry.get("placed_utc")) if placed else None
        # As the form's own rule: placed after its alert and before its game (Codex on #276).
        if when is not None and not alert["alert_utc"] <= when < alert["start_utc"]:
            raise ValueError(f"a bet placed outside its alert and its game's start: {key}")
        matched = float(entry["matched_price"]) if placed else None
        stake = float(entry["stake"]) if placed else None
        allowed = stake_at(json.loads(alert["stakes"]), matched) if matched is not None else None
        entered = _instant(entry.get("entered_utc"))
        late = entry.get("late")
        if late is None and entered is not None:
            late = entered >= alert["start_utc"]
        rows.append(
            {
                **{name: alert[name] for name in dtypes(RealBets) if name in alert},
                "outcome": outcome,
                "reason": None if placed else entry.get("reason"),
                "matched_price": matched,
                "stake": stake,
                "bet_id": str(entry["bet_id"]) if placed else None,
                "placed_utc": when,
                "entered_utc": entered,
                "late": late,
                "allowed_stake": allowed,
                "within_rule": None
                if not placed
                else bool(
                    matched is not None
                    and stake is not None
                    and allowed is not None
                    and matched >= alert["min_price"] - 1e-9
                    and stake <= allowed + 1e-9
                ),
                "corrections": counts.get(key, 0),
            }
        )
    built = pl.DataFrame(rows, schema=dtypes(RealBets))
    return RealBets.validate(built)  # type: ignore[return-value]


def results(placed: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """The placed bets whose game is final, with its result on the full game (OT and shootout
    included), when it became public (result_utc), and the profit at the matched price and stake;
    a game played more than POSTPONED from its listed start voids its bet."""
    played = games.select("game_id", played_utc="start_utc").join(outcomes(games), on="game_id")
    final = placed.join(played, on="game_id")
    void = (pl.col("played_utc") - pl.col("start_utc")).abs() > POSTPONED
    won = staking.won(pl.col("side"), pl.col("home_win"))
    return final.with_columns(
        status=pl.when(void).then(pl.lit(VOID)).otherwise(pl.lit(SETTLED)),
        home_win=pl.when(void).then(None).otherwise(pl.col("home_win")),
        won=pl.when(void).then(None).otherwise(won),
        profit=pl.when(void)
        .then(0.0)
        .when(won)
        .then(pl.col("stake") * (pl.col("matched_price") - 1))
        .otherwise(-pl.col("stake")),
    )


def bankroll(record: pl.DataFrame, games: pl.DataFrame, decision_utc: datetime, of: str) -> float:
    """A mode's bankroll at a decision: 100 € plus the profit of its placed bets whose results
    were public before it."""
    placed = record.filter(pl.col("outcome") == PLACED, pl.col("mode") == of)
    known = results(placed, games).filter(pl.col("result_utc") < decision_utc)
    return real.BANKROLL + (float(known["profit"].sum()) if known.height else 0.0)


def settle(
    record: pl.DataFrame,
    games: pl.DataFrame,
    stored: pl.DataFrame,
    now: datetime,
    code_version: str,
) -> pl.DataFrame:
    """The settlements to append: each placed bet whose result is public by now and has none
    yet, or whose latest one no longer matches its corrected entry (its next revision); a bet a
    correction left unplaced after its settlement gets a cancelled revision. bankroll_after
    counts every bet's latest revision of its mode, in result order."""
    columns = dtypes(RealSettlements)
    placed = record.filter(pl.col("outcome") == PLACED)
    current = results(placed, games).filter(pl.col("result_utc") <= now)
    latest: dict[tuple[date, int], dict[str, Any]] = {}
    for row in stored.sort("revision").iter_rows(named=True):
        latest[(row["game_date"], row["game_id"])] = row
    profits: dict[str, dict[tuple[date, int], float]] = {PRACTICE: {}, REAL: {}}
    for key, row in latest.items():
        profits[row["mode"]][key] = row["profit"]
    compared = ("mode", "side", "matched_price", "stake", "bet_id", "status")
    due = []
    for row in current.sort("result_utc", "game_id").iter_rows(named=True):
        before = latest.get((row["game_date"], row["game_id"]))
        if (
            before is not None
            and all(before[name] == row[name] for name in compared)
            and abs(before["profit"] - row["profit"]) < 1e-9
        ):
            continue
        due.append((row, before))
    still_placed = set(zip(placed["game_date"].to_list(), placed["game_id"].to_list(), strict=True))
    for key, before in latest.items():
        if before["status"] != CANCELLED and key not in still_placed:
            due.append((before | {"status": CANCELLED, "home_win": None, "won": None}, before))
    rows = []
    for row, before in due:
        key = (row["game_date"], row["game_id"])
        profit = 0.0 if row["status"] == CANCELLED else row["profit"]
        if before is not None and before["mode"] != row["mode"]:
            profits[before["mode"]].pop(key, None)
        profits[row["mode"]][key] = profit
        rows.append(
            {
                **{name: row[name] for name in ("game_date", "season", "game_id", "mode")},
                **{name: row[name] for name in ("venue", "side", "matched_price", "stake")},
                "revision": 0 if before is None else before["revision"] + 1,
                "bet_id": row["bet_id"],
                "status": row["status"],
                "home_win": row["home_win"],
                "won": row["won"],
                "profit": profit,
                "result_utc": row["result_utc"],
                "settled_utc": now,
                "bankroll_after": real.BANKROLL + sum(profits[row["mode"]].values()),
                "code_version": code_version,
            }
        )
    built = pl.DataFrame(rows, schema=columns)
    return RealSettlements.validate(built)  # type: ignore[return-value]


def write_settlements(store: lb.Store, settled: pl.DataFrame) -> list[str]:
    """Append each settlement once."""
    keys = []
    for row in settled.iter_rows(named=True):
        key = settlement_key(row["game_date"], row["game_id"], row["revision"])
        store.put(
            key,
            lb.parquet(
                settled.filter(
                    (pl.col("game_date") == row["game_date"])
                    & (pl.col("game_id") == row["game_id"])
                    & (pl.col("revision") == row["revision"])
                )
            ),
        )
        keys.append(key)
    return keys


def settlements(store: lb.Store) -> pl.DataFrame:
    """Every settlement appended (the lake's real_settlements)."""
    import io

    columns = dtypes(RealSettlements)
    parts = [
        pl.read_parquet(io.BytesIO(store.get(key))) for key in store.keys(f"{PREFIX}/settlements")
    ]
    if not parts:
        return pl.DataFrame(schema=columns)
    every = pl.concat(parts).cast(columns)  # type: ignore[arg-type]
    return RealSettlements.validate(every.sort("game_date", "game_id", "revision"))  # type: ignore[return-value]


def statement(record: pl.DataFrame, settled: pl.DataFrame, of: str) -> list[str]:
    """A mode's placed bets with their settlement and the bankroll after, for the weekly check
    against the venue's statement."""
    lines = []
    latest = (
        settled.filter(pl.col("mode") == of)
        .sort("revision")
        .group_by("game_date", "game_id", maintain_order=True)
        .last()
    )
    placed = record.filter(pl.col("mode") == of, pl.col("outcome") == PLACED)
    # The alert's own status travels with the bet, so the settlement's is named apart.
    joined = placed.join(
        latest.select("game_date", "game_id", "profit", "bankroll_after", settlement="status"),
        on=["game_date", "game_id"],
        how="left",
    ).sort("game_date", "game_id")
    for row in joined.iter_rows(named=True):
        team = row["home"] if row["side"] == "home" else row["away"]
        result = (
            "awaiting the result"
            if row["settlement"] is None
            else f"{row['settlement']}, {row['profit']:+.2f} €, bankroll "
            f"{row['bankroll_after']:.2f} €"
        )
        flags = "" if row["within_rule"] else " (outside the rule)"
        flags += " (recorded late)" if row["late"] else ""
        lines.append(
            f"{row['game_date']} {row['away']} at {row['home']}: {team} at "
            f"{_price(row['matched_price'])}, {row['stake']:.2f} €, bet id {row['bet_id']}: "
            f"{result}{flags}"
        )
    total = real.BANKROLL + (float(latest["profit"].sum()) if latest.height else 0.0)
    lines.append(f"{of} bankroll from the settlements: {total:.2f} €")
    return lines
