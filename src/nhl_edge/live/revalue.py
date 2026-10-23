"""Re-valuing a resting maker part (ADR 0045 item 2, #321): the bid follows fresh information down,
never up.

**When.** At each odds slot after the order went, with the slot's own snapshot (`--slot`), and every
15 minutes from the decision to the expiry (kalshi-revalue.yml), on either trigger:
- **Kalshi's move:** the market's best Yes bid or ask moved MOVE or more since the part's last
  valuation, by #319's watch (its newest stored top of book; before any valuation, the last top
  stored before the part was placed). Kalshi's prices are a trigger only, never an input to p'.
- **A starter change:** Daily Faceoff names another starter for either team than it named at the
  decision. A team it named none for then has no starter to change.

**The fresh price.** A slot values every resting part with the slot's snapshot. A Kalshi move needs
Pinnacle's price now: one Odds API h2h call in the EU region (1 credit, every game) for the parts
the run's triggers name, at most CALLS_PER_DAY a day, each call claimed once in R2
(real/revalue-calls/<ET date>/<n>.json); past the cap the trigger waits for the next slot. The
response is stored raw under odds-revalue/, apart from odds/, so it never enters odds_snapshots or
the closing proxy.

**The value p'** (hard rule 3): the frozen live blend (BLEND) with Pinnacle's fresh pair de-vigged
multiplicatively (market/devig.py) in place of the decision's, and the decision's B3 and u (the
day's ledger), at the decision's hurdle. A quote last updated over ADR 0033's 5 minutes before the
valuation is none: the part rests as it is.

**The rule.** L' is the highest cent at or below the resting limit at which the part's resting
contracts clear the hurdle at p' with the maker fee:
- no cent: the part is cancelled;
- L' below the resting limit: the bid is lowered to L' (Kalshi's amend, the filled count kept);
- otherwise it rests as it is. Never raised.

A starter change cancels the part outright, with no price.

**The record.** Each valuation is written once beside its order (...-reval-<stamp>.json): its time,
trigger, p', action, limits before and after, the resting contracts, the quote and the top of
book it read, and the starters. The owner's Telegram hears of each bid lowered or cancelled. The
log prints counts only (run logs are public).
"""

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import polars as pl
from pandera.errors import SchemaError

from nhl_edge.backtest.walk_forward import refused
from nhl_edge.ingest import dailyfaceoff as dfo
from nhl_edge.ingest import kalshi
from nhl_edge.ingest import kalshi_watch as kw
from nhl_edge.ingest.nhl_api import parse_utc
from nhl_edge.ingest.odds import ET, OddsApi, OddsApiError, parse_odds
from nhl_edge.ingest.odds_lake import match_games
from nhl_edge.lake.raw import RawStore
from nhl_edge.live import blend_fit, real
from nhl_edge.live import bundle as lb
from nhl_edge.live import placement as pm
from nhl_edge.live.exchange import cost as order_cost
from nhl_edge.live.kalshi_trade import KalshiTrade, KalshiTradeError
from nhl_edge.live.predict import BOOK, MAX_QUOTE_AGE, devig

SLOT, MOVED, STARTER = "slot", "kalshi move", "starter change"
REST, LOWER, CANCEL, STALE = "rest", "lower", "cancel", "no fresh quote"
MOVE = Decimal("0.03")
CALLS_PER_DAY = 4
CALLS = "real/revalue-calls"
SOURCE = "odds-revalue"
REGIONS = "eu"
# A top of book this old at a check moves nothing: the watch isn't running.
TOP_AGE = timedelta(minutes=10)
TOPS_PREFIX = f"{kalshi.SOURCE}/{kw.WATCH}/{kw.TOPS}"
# The polls a check looks back through for one whose responses came before it.
POLLS_BACK = 3


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class Resting:
    """A maker part resting on Kalshi, with what values it: the decision's B3, u and hurdle, and
    its last valuation, if any."""

    env: str
    game_date: date
    game_id: int
    team: str
    home: str
    away: str
    ticker: str
    start_utc: datetime
    expires_utc: datetime
    placed_utc: datetime
    order_id: str
    client_order_id: str
    limit: Decimal
    hurdle: float
    p_b3: float
    u: float
    prediction_utc: datetime
    last: Mapping[str, Any] | None

    @property
    def is_home(self) -> bool:
        return self.team == self.home


def resting(
    store: lb.Store,
    env: str,
    ledger: Callable[[date], pl.DataFrame | None],
    now: datetime,
) -> tuple[list[Resting], int]:
    """The maker parts of an environment still resting by the record, soonest game first, and how
    many lack their decision's inputs in the day's ledger. A part cancelled by a valuation, final,
    of a game the schedule moved earlier (the reconcile cancels it), or due to expire within LEAD
    is none."""
    found, missing = [], 0
    for (day, game_id), record in sorted(pm.stored_orders(store, env).items()):
        order, maker = record.get("order"), record.get(pm.MAKER_PLACED)
        if order is None or maker is None or not pm.two_parts(order) or "final" in record:
            continue
        if any(name.startswith(pm.MOVED) for name in record):
            continue
        done = pm.valuations(record)
        if any(v["action"] == CANCEL for v in done):
            continue
        expires = _instant(order["expires_utc"])
        if now >= expires - pm.LEAD:
            continue
        rows = ledger(date.fromisoformat(day))
        row = None
        if rows is not None:
            picked = rows.filter(pl.col("game_id") == game_id)
            row = picked.row(0, named=True) if picked.height else None
        if row is None or row.get("p_b3") is None or row.get("u") is None:
            missing += 1
            continue
        lowered = [v for v in done if v["action"] == LOWER]
        found.append(
            Resting(
                env=env,
                game_date=date.fromisoformat(day),
                game_id=game_id,
                team=order["team"],
                home=row["home"],
                away=row["away"],
                ticker=order["ticker"],
                start_utc=_instant(order["start_utc"]),
                expires_utc=expires,
                placed_utc=_instant(maker["placed_utc"]),
                order_id=lowered[-1]["order_id"] if lowered else maker["order_id"],
                client_order_id=maker["client_order_id"],
                limit=Decimal(lowered[-1]["limit_after"] if lowered else maker["limit"]),
                hurdle=float(order["hurdle"]),
                p_b3=float(row["p_b3"]),
                u=float(row["u"]),
                prediction_utc=row["prediction_utc"],
                last=done[-1] if done else None,
            )
        )
    found.sort(key=lambda part: (part.start_utc, part.game_id))
    return found, missing


@dataclass(frozen=True)
class Quote:
    """Pinnacle's h2h pair of a game in a snapshot, with the older side's last update and the
    Odds API's start of the game."""

    snapshot_utc: datetime
    last_update_utc: datetime
    home_price: float
    away_price: float
    raw_key: str
    commence_utc: datetime

    def fresh(self, at: datetime) -> bool:
        """Taken before at, last updated within ADR 0033's 5 minutes of it, and for a game not
        under way at it by the Odds API's own start, as odds.available_at reads one: the blend
        was fitted on pre-game prices."""
        return (
            self.snapshot_utc <= at
            and at - self.last_update_utc <= MAX_QUOTE_AGE
            and self.commence_utc > at
        )

    def record(self) -> dict[str, Any]:
        return {
            "snapshot_utc": self.snapshot_utc.isoformat(),
            "last_update_utc": self.last_update_utc.isoformat(),
            "commence_utc": self.commence_utc.isoformat(),
            "home_price": self.home_price,
            "away_price": self.away_price,
            "raw_key": self.raw_key,
        }


def quotes(
    body: bytes, snapshot_utc: datetime, raw_key: str, parts: Sequence[Resting]
) -> dict[int, Quote]:
    """Pinnacle's h2h pair of each part's game in a stored Odds API response. A pair de-vigging
    refuses (implied probabilities summing below 100%) is no price, as predict.devigable reads
    one, so its part rests."""
    rows = parse_odds(body, snapshot_utc, "revalue", raw_key)
    listings = pl.DataFrame(
        [
            {
                "game_id": p.game_id,
                "game_type": 2,
                "start_utc": p.start_utc,
                "home": p.home,
                "away": p.away,
            }
            for p in parts
        ],
        schema={
            "game_id": pl.Int64,
            "game_type": pl.Int8,
            "start_utc": pl.Datetime("us", "UTC"),
            "home": pl.String,
            "away": pl.String,
        },
    )
    h2h = match_games(rows, listings).filter(
        pl.col("book") == BOOK, pl.col("market") == "h2h", pl.col("game_id").is_not_null()
    )
    found = {}
    for game_id in h2h["game_id"].unique().to_list():
        sides = {
            r["side"]: r for r in h2h.filter(pl.col("game_id") == game_id).iter_rows(named=True)
        }
        if "home" not in sides or "away" not in sides:
            continue
        home_price = float(sides["home"]["price_decimal"])
        away_price = float(sides["away"]["price_decimal"])
        if refused(pl.DataFrame({"home_price": [home_price], "away_price": [away_price]}))[0]:
            continue
        found[int(game_id)] = Quote(
            snapshot_utc=snapshot_utc,
            last_update_utc=min(sides["home"]["last_update_utc"], sides["away"]["last_update_utc"]),
            home_price=home_price,
            away_price=away_price,
            raw_key=raw_key,
            commence_utc=min(
                sides["home"]["commence_time_utc"], sides["away"]["commence_time_utc"]
            ),
        )
    return found


def p_side(part: Resting, quote: Quote, live: blend_fit.LiveFit) -> float:
    """p': the frozen blend at Pinnacle's fresh pair de-vigged, with the decision's B3 and u, on
    the part's team."""
    pair = pl.DataFrame({"home_price": [quote.home_price], "away_price": [quote.away_price]})
    p_b0 = devig(pair).to_numpy()
    p_home = float(live.blends["BLEND"].predict(p_b0, np.array([part.p_b3]), np.array([part.u]))[0])
    return p_home if part.is_home else 1 - p_home


def amend_id(part: Resting, price: Decimal) -> str:
    """The client order id a part's bid takes when lowered to a price: the same for the same part
    and cent, and each cent's its own, since a bid is only ever lowered."""
    name = f"{part.client_order_id}:amend:{round(price * 100)}"
    return str(uuid.uuid5(pm.NAMESPACE, name))


def lowered(
    p: float, hurdle: float, limit: Decimal, contracts: Decimal, rules: real.Exchange
) -> Decimal | None:
    """L': the highest cent at or below the limit at which the contracts, resting at the maker
    fee, clear the hurdle at p (hard rule 4), or None when no cent does."""
    for cents in range(round(limit * 100), 0, -1):
        price = Decimal(cents) / 100
        total = order_cost(contracts, price, rules, maker=True)
        if p * float(contracts / total) - 1 >= hurdle:
            return price
    return None


def top_at(raw: RawStore, part: Resting, at: datetime) -> dict[str, Any] | None:
    """The part's market's best Yes bid and ask in the watch's newest top-of-book poll stored at
    or before at, fetched before at and before the start, or None. A poll's key is stamped when
    it began, so a poll whose responses came after at gives way to the one before it, up to
    POLLS_BACK polls."""
    prefix, before = f"{TOPS_PREFIX}/{part.game_date.isoformat()}", at
    for _ in range(POLLS_BACK):
        key = raw.latest_before(prefix, before)
        if key is None:
            return None
        found = _top_in(raw, key, part, at)
        if found is not None:
            return found
        began = datetime.strptime(key.rsplit("/", 1)[-1][:16], "%Y%m%dT%H%M%SZ")
        before = began.replace(tzinfo=UTC) - timedelta(seconds=1)
    return None


def _top_in(raw: RawStore, key: str, part: Resting, at: datetime) -> dict[str, Any] | None:
    """The part's market's top of book in one stored poll, from a response fetched before at."""
    try:
        meta = raw.meta(key)
        markets = {m["ticker"]: kalshi.market_of(m) for m in meta["markets"]}
        polled = json.loads(raw.get(key))["responses"]
    except (KeyError, TypeError, ValueError):
        return None
    for response in polled:
        if response.get("invalid") or response.get("status") != kalshi.OK:
            continue
        fetched = parse_utc(response["fetched_utc"])
        if fetched > at:
            continue
        try:
            rows = kw.parse_tops(response["body"].encode(), markets, fetched, key)
        except (kalshi.KalshiError, KeyError, TypeError, ValueError, SchemaError):
            continue
        row = rows.filter(pl.col("ticker") == part.ticker)
        if row.height:
            found = row.row(0, named=True)
            return {
                "yes_bid": found["yes_bid"],
                "yes_ask": found["yes_ask"],
                "observed_utc": fetched.isoformat(),
                "raw_key": key,
            }
    return None


def fresh_top(raw: RawStore, part: Resting, at: datetime) -> dict[str, Any] | None:
    """top_at, or None when it was observed over TOP_AGE before at: the watch isn't running."""
    found = top_at(raw, part, at)
    if found is None or at - _instant(found["observed_utc"]) > TOP_AGE:
        return None
    return found


def moved(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> bool:
    """A move of MOVE or more in the best Yes bid or the best Yes ask between two tops of book.
    A side missing from either has nothing to compare."""
    if before is None or after is None:
        return False
    for side in ("yes_bid", "yes_ask"):
        a, b = before.get(side), after.get(side)
        if a is not None and b is not None and abs(Decimal(str(b)) - Decimal(str(a))) >= MOVE:
            return True
    return False


def starters(raw: RawStore, part: Resting, at: datetime) -> dict[str, int | None] | None:
    """Each team's starter Daily Faceoff named (its id) in its newest page stored at or before
    at, among games not started then, or None without a page. Its listed start can differ by
    minutes from the schedule's, so each team's game is the one nearest the part's start, as
    confirmed.calls matches them."""
    key = raw.latest_before(f"{dfo.PREFIX}/{part.game_date.isoformat()}", at)
    if key is None:
        return None
    try:
        observed = parse_utc(raw.meta(key)["fetched_utc"])
        rows = dfo.parse_starting_goalies(raw.get(key), observed, key)
    except (dfo.DailyFaceoffError, KeyError, TypeError, ValueError, SchemaError):
        return None
    if observed > at:
        return None
    named: dict[str, int | None] = {}
    for team in (part.home, part.away):
        listed = rows.filter(pl.col("team") == team)
        if listed.height:
            gap = (pl.col("start_utc") - part.start_utc).abs()
            named[team] = listed.sort(gap).row(0, named=True)["dfo_goalie_id"]
    return {team: named.get(team) for team in (part.home, part.away)}


def changed(decided: Mapping[str, int | None] | None, now: Mapping[str, int | None] | None) -> bool:
    """Whether Daily Faceoff names another starter for either team than at the decision."""
    if decided is None or now is None:
        return False
    return any(
        decided.get(team) is not None and now.get(team) is not None and now[team] != decided[team]
        for team in decided
    )


def claim_call(store: lb.Store, day: date, now: datetime) -> bool:
    """Claim one of the ET day's triggered Odds API calls, at most CALLS_PER_DAY: each claim is
    written once, so two runs never take the same one."""
    prefix = f"{CALLS}/{day.isoformat()}"
    for _ in range(CALLS_PER_DAY + 1):
        taken = len(store.keys(prefix))
        if taken >= CALLS_PER_DAY:
            return False
        try:
            lb.put_once(
                store,
                f"{prefix}/{taken + 1}.json",
                json.dumps({"claimed_utc": now.isoformat()}).encode(),
            )
        except lb.Conflict:
            continue
        return True
    return False


@dataclass
class RevalueReport:
    resting: int = 0
    missing: int = 0
    valued: int = 0
    lowered: int = 0
    cancelled: int = 0
    rested: int = 0
    stale: int = 0
    moved: int = 0
    starters: int = 0
    waiting: int = 0
    calls: int = 0
    failed: int = 0
    records: list[dict[str, Any]] = field(default_factory=list)

    def line(self, env: str) -> str:
        return (
            f"kalshi revalue {env}: {self.resting} resting ({self.missing} without their "
            f"decision's inputs), {self.valued} valued: {self.lowered} lowered, "
            f"{self.cancelled} cancelled, {self.rested} rest, {self.stale} without a fresh "
            f"quote; triggers: {self.moved} Kalshi moves, {self.starters} starter changes, "
            f"{self.waiting} waiting for the next slot; {self.calls} Odds API calls, "
            f"{self.failed} failed"
        )


@dataclass(frozen=True)
class Snapshot:
    """A stored Odds API response a run values with."""

    body: bytes
    snapshot_utc: datetime
    raw_key: str


def _write(
    store: lb.Store, part: Resting, now: datetime, body: Mapping[str, Any]
) -> dict[str, Any]:
    stamp = f"{pm.REVAL}-{now:%Y%m%dT%H%M%S%fZ}"
    key = pm.order_key(part.env, part.game_date, part.game_id, stamp)
    record = {"valued_utc": now.isoformat(), **body}
    lb.put_once(store, key, json.dumps(record, sort_keys=True).encode())
    return record | {"part": part}


def value(
    store: lb.Store,
    trade: KalshiTrade,
    live: blend_fit.LiveFit,
    rules: real.Exchange,
    part: Resting,
    quote: Quote | None,
    trigger: str,
    top: Mapping[str, Any] | None,
    now: datetime,
    report: RevalueReport,
) -> None:
    """Value a resting part with a quote and act by the rule; the record is written once after
    Kalshi's answer. A part Kalshi no longer shows resting is the reconcile's."""
    try:
        shown = trade.read_order(part.order_id, lambda _: True)
    except KalshiTradeError:
        report.failed += 1
        return
    if shown is None or shown.get("status") != "resting":
        return
    remaining = Decimal(str(shown.get("remaining_count_fp") or 0))
    filled = Decimal(str(shown.get("fill_count_fp") or 0))
    if remaining <= 0:
        return
    current = part.limit
    if shown.get("yes_price_dollars"):
        current = min(current, Decimal(str(shown["yes_price_dollars"])).quantize(Decimal("0.01")))
    base = {
        "trigger": trigger,
        "limit_before": f"{current:.2f}",
        "remaining": f"{remaining:.2f}",
        "quote": quote.record() if quote is not None else None,
        "top": dict(top) if top is not None else None,
    }
    if quote is None or not quote.fresh(now):
        report.stale += 1
        report.records.append(
            _write(
                store,
                part,
                now,
                base
                | {"action": STALE, "p_side": None, "limit_after": None, "order_id": part.order_id},
            )
        )
        return
    p = p_side(part, quote, live)
    target = lowered(p, part.hurdle, current, remaining, rules)
    order_id = part.order_id
    try:
        if target is None:
            if trade.cancel(part.order_id, part.ticker) is None:
                return  # no longer resting since its read: the reconcile's (Codex on #327)
            action = CANCEL
        elif target < current:
            renamed = amend_id(part, target)
            try:
                answer = trade.amend(
                    part.order_id, part.ticker, target, filled + remaining, renamed
                )
            except KalshiTradeError:
                # An answer lost after Kalshi applied the amend, which may have replaced the
                # order: the bid is found by its new client order id, else the run failed.
                answer = trade.find_order(part.ticker, renamed)
                if answer is None:
                    raise
            order_id, action = str(answer["order_id"]), LOWER
        else:
            action = REST
    except KalshiTradeError:
        report.failed += 1
        return
    report.valued += 1
    report.lowered += action == LOWER
    report.cancelled += action == CANCEL
    report.rested += action == REST
    after = None if target is None else f"{min(target, current):.2f}"
    report.records.append(
        _write(
            store,
            part,
            now,
            base
            | {
                "action": action,
                "p_side": round(p, 6),
                "limit_after": after if action != CANCEL else None,
                "order_id": order_id,
            },
        )
    )


def revalue(
    store: lb.Store,
    raw: RawStore,
    trade: KalshiTrade,
    live: blend_fit.LiveFit,
    rules: real.Exchange,
    parts: Sequence[Resting],
    now: Callable[[], datetime],
    *,
    slot: Snapshot | None = None,
    odds: OddsApi | None = None,
) -> RevalueReport:
    """One run over the resting parts. A starter change cancels its part first. With a slot's
    snapshot, every other part placed before it is valued with it; without one, the parts whose
    market moved get one Odds API call, claimed within the day's cap, or wait for the next slot."""
    report = RevalueReport(resting=len(parts))
    left = []
    for part in parts:
        at = now()
        decided = starters(raw, part, part.prediction_utc)
        current = starters(raw, part, at)
        if not changed(decided, current):
            left.append(part)
            continue
        report.starters += 1
        try:
            cancelled = trade.cancel(part.order_id, part.ticker)
        except KalshiTradeError:
            report.failed += 1
            continue
        if cancelled is None:
            continue  # no longer resting, filled or gone since: the reconcile's (Codex on #327)
        report.cancelled += 1
        report.records.append(
            _write(
                store,
                part,
                at,
                {
                    "trigger": STARTER,
                    "action": CANCEL,
                    "p_side": None,
                    "limit_before": f"{part.limit:.2f}",
                    "limit_after": None,
                    "order_id": part.order_id,
                    "quote": None,
                    "top": None,
                    "starters": {"decision": decided, "now": current},
                },
            )
        )
    if slot is not None:
        found = quotes(slot.body, slot.snapshot_utc, slot.raw_key, left)
        for part in left:
            if slot.snapshot_utc <= part.placed_utc:
                continue  # the order went after this snapshot, which the decision already read
            at = now()
            value(
                store,
                trade,
                live,
                rules,
                part,
                found.get(part.game_id),
                SLOT,
                fresh_top(raw, part, at),
                at,
                report,
            )
        return report
    triggered = []
    for part in left:
        at = now()
        # The baseline: the last valuation's top, else the fresh one at its time, so a move it
        # already priced in isn't seen again (Codex on #327); before any, the top at placement.
        if part.last is not None:
            before = part.last.get("top") or fresh_top(raw, part, _instant(part.last["valued_utc"]))
        else:
            before = fresh_top(raw, part, part.placed_utc)
        after = fresh_top(raw, part, at)
        if moved(before, after):
            triggered.append((part, after))
    report.moved = len(triggered)
    if not triggered:
        return report
    if odds is None or not claim_call(store, now().astimezone(ET).date(), now()):
        report.waiting = len(triggered)
        return report
    try:
        response = odds.odds(REGIONS, ("h2h",))
    except OddsApiError:
        report.failed += 1
        return report
    report.calls += 1
    stamp = f"{response.snapshot_utc:%Y%m%dT%H%M%SZ}"
    raw_key = raw.put(
        SOURCE,
        f"{response.snapshot_utc:%Y-%m-%d}/{stamp}_revalue_{REGIONS}",
        response.body,
        {
            "fetched_utc": response.snapshot_utc.isoformat(),
            "endpoint": "/v4/sports/icehockey_nhl/odds",
            "params": response.params,
            "credits": vars(response.credits),
        },
    )
    found = quotes(response.body, response.snapshot_utc, raw_key, [p for p, _ in triggered])
    for part, top in triggered:
        value(store, trade, live, rules, part, found.get(part.game_id), MOVED, top, now(), report)
    return report


def _cents(value: str | None) -> str:
    return f"{round(float(value) * 100)}¢" if value else "?"


def message(env: str, records: Sequence[Mapping[str, Any]]) -> str | None:
    """The owner's message of the bids lowered or cancelled and not told yet, or None."""
    told = [r for r in records if r["action"] in (LOWER, CANCEL)]
    if not told:
        return None
    header = "DEMO, mock funds" if env == pm.DEMO else "real orders"
    lines = [f"NHL Edge · Kalshi {header}: bids re-valued"]
    for record in told:
        name = f"{record['game_date']} {record['team']}"
        if record["trigger"] == STARTER:
            lines.append(f"{name}: bid cancelled, Daily Faceoff names another starter")
        elif record["action"] == CANCEL:
            lines.append(
                f"{name}: bid cancelled, no cent clears the hurdle at p {record['p_side']:.3f} "
                f"({record['trigger']})"
            )
        else:
            lines.append(
                f"{name}: bid lowered from {_cents(record['limit_before'])} to "
                f"{_cents(record['limit_after'])}, p {record['p_side']:.3f} ({record['trigger']})"
            )
    return "\n".join(lines)


def unnotified(store: lb.Store, env: str) -> list[dict[str, Any]]:
    """The valuations that lowered or cancelled a bid whose message hasn't gone yet, each with
    its game date, id, team and record part's name."""
    found = []
    for (day, game_id), record in sorted(pm.stored_orders(store, env).items()):
        order = record.get("order")
        if order is None:
            continue
        for name, body in sorted(record.items()):
            if not name.startswith(f"{pm.REVAL}-") or body.get("action") not in (LOWER, CANCEL):
                continue
            if f"{pm.NOTIFIED}_{name}" in record:
                continue
            found.append(
                body
                | {
                    "day": day,
                    "game_id": game_id,
                    "game_date": day,
                    "team": order["team"],
                    "name": name,
                }
            )
    return found


def mark_notified(
    store: lb.Store, env: str, records: Sequence[Mapping[str, Any]], now: datetime
) -> None:
    """Mark each valuation's message as sent, once."""
    for record in records:
        key = pm.order_key(
            env,
            date.fromisoformat(record["day"]),
            record["game_id"],
            f"{pm.NOTIFIED}_{record['name']}",
        )
        if not pm._held(store, key):
            lb.put_once(store, key, json.dumps({"notified_utc": now.isoformat()}).encode())


def ledger_of(store: lb.Store) -> Callable[[date], pl.DataFrame | None]:
    """A reader of each day's paper ledger (ledger/live/<date>.parquet), the decision's inputs."""
    import io

    from nhl_edge.live.predict import ledger_key

    cache: dict[date, pl.DataFrame | None] = {}

    def read(day: date) -> pl.DataFrame | None:
        if day not in cache:
            key = ledger_key(day)
            held = key in store.keys(key.rsplit("/", 1)[0])
            cache[day] = pl.read_parquet(io.BytesIO(store.get(key))) if held else None
        return cache[day]

    return read
