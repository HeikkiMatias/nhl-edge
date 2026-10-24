"""Shadow kalshi_prices (#320, ADR 0046): select on Kalshi's own prices, two fair values against
one execution and one budget. A paper shadow only: no paper bet, real bet or policy reads it.

**Opportunities.** Both teams' Yes markets of every game in the day's ledger, at each odds slot
from the midday decision to the start (SLOTS). Each needs Pinnacle's pair fresh by ADR 0033 (its
older side updated within MAX_QUOTE_AGE of the snapshot and within MAX_PUBLISHED_AGE of the slot's
time) and Kalshi's depth for the market observed within MAX_BOOK_AGE before it. A slot acts at its
snapshot, or at the decision's publication when that is later (the midday slot), so every input it
reads was observed by then (hard rule 1). Never at or after the start (hard rule 2).

**Two arms on the same opportunities:**
- **market:** Pinnacle's pair at the slot, de-vigged multiplicatively (market/devig.py, hard rule
  3), with the frozen 2.5% hurdle and quarter Kelly (ADR 0028), u⁺ = 0;
- **hockey:** conservative_blend version 1 (ADR 0041) with its fit for the week, at the slot's
  market probability and the decision's p_B3, with the hurdle and the stake moved by the
  decision's u as ADR 0028 moves them. A week the blend is unfitted has no hockey opportunities.
  From the first slot at which Daily Faceoff names another starter for either team than at the
  decision, the game leaves the arm, and a resting bid of it is cancelled.

**Execution, the same in both arms (live/exchange.py's arithmetic):**
- **taker:** the market's Yes asks in the depth at the slot, cheapest first, each level at the
  taker fee rounded up: the most contracts the stake allows whose cost clears the hurdle,
  p · C / cost - 1 ≥ hurdle (hard rule 4), filled at once, an upper bound;
- **maker:** then a post-only bid for what the stake leaves, at the highest cent that clears the
  hurdle at the maker fee (exchange.order), kept a cent below the best ask, sized after the taker
  part as exchange.rest sizes a real one. It rests until the expiry, re-valued at each later slot
  as live/revalue.py re-values a real one: lowered or cancelled, never raised. It fills when the
  trades after its posting at or below its price exceed the depth ahead of it at its price at
  posting, or when a top of book shows an ask at or below it. Partial fills count; an upper bound.
- **No ladder:** one maker bid per arm, game and team.

**One budget per arm:** a notional BANKROLL with the frozen policy's caps: at most max_bet of it on
a game across every hook, one team per game (the first to clear), and max_day a day. A resting
bid counts as filled until it is cancelled. Hooks go slot by slot; within a slot by expected
return at the best ask, highest first (hard rule 4); taker before maker.

**Measures** (summary), per arm and for taker and maker apart, each through the live report's
estimate (its weekly block bootstrap interval, counts only under 4 weeks, hard rule 7): the fills
by reference status; net CLV per bid against #318's reference (live/fills.py: Pinnacle's closing
proxy observed after the fill; a fill at or after it is counted apart, never valued against an
earlier quote); the expected profit captured in euros; the paired difference, hockey minus market,
per opportunity on the days both arms ran; and the share of maker fills after a starter change or
after Pinnacle's fair price moved against the side since the bid's posting.

**Private.** It reads Kalshi's books and trades (#280): every figure goes to the record's store
under PREFIX, and the log prints counts only.
"""

import io
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

import numpy as np
import polars as pl

from nhl_edge.betting.selection import POLICY
from nhl_edge.ingest import kalshi_watch as kw
from nhl_edge.live import bundle as lb
from nhl_edge.live import challengers
from nhl_edge.live import exchange as ex
from nhl_edge.live import fills as lf
from nhl_edge.live import report as lr
from nhl_edge.live import revalue as rv
from nhl_edge.live.predict import BOOK, MAX_PUBLISHED_AGE, MAX_QUOTE_AGE, PREDICTED
from nhl_edge.live.real import Exchange
from nhl_edge.live.shadow import _named
from nhl_edge.market.devig import fair_probabilities
from nhl_edge.market.recalibration import logit, sigmoid

SHADOW, VERSION, ADR = "kalshi_prices", "1", "0046"
PREFIX = f"real/shadow/{SHADOW}/{VERSION}"
# The first game date the shadow reads: its rules were fixed that day, before any reading.
FIRST_DAY = date(2026, 10, 24)
SLOTS = ("midday", "pre7", "pre8", "pre10")
MARKET, HOCKEY = "market", "hockey"
ARMS = (MARKET, HOCKEY)
TAKER, MAKER = lf.TAKER, lf.MAKER
KINDS = (TAKER, MAKER)
BANKROLL = 100.0
# The watch polls the depth every 5 minutes (#319): an older book is no slot's.
MAX_BOOK_AGE = kw.BOOK_EVERY
# The lake tables a run reads: the slots' odds, the decisions and results (the hockey fit, ADR
# 0041), Daily Faceoff's polls and the watch's logs (#319).
TABLES = (
    "odds_snapshots",
    "paper_ledger",
    "games",
    "dailyfaceoff_goalies",
    "kalshi_depth",
    "kalshi_trades",
    "kalshi_tops",
)
CENT = Decimal("0.01")

OPPORTUNITY_SCHEMA = {
    "arm": pl.String,
    "slot": pl.String,
    "at_utc": pl.Datetime("us", UTC),
    "game_date": pl.Date,
    "season": pl.Int32,
    "game_id": pl.Int64,
    "team": pl.String,
    "start_utc": pl.Datetime("us", UTC),
    "p_side": pl.Float64,
    "hurdle": pl.Float64,
    "action": pl.String,
}
FILL_SCHEMA = lf.FILL_SCHEMA | {
    "arm": pl.String,
    "starter_changed": pl.Boolean,
    "moved_against": pl.Boolean,
}
# What a hook did at its opportunity, for the counts.
TOOK, POSTED, BOTH, NONE, HELD, NO_BOOK, OTHER_TEAM = (
    "took",
    "posted",
    "took and posted",
    "nothing clears",
    "budget used",
    "no fresh book",
    "other team held",
)


def _utc(column: str) -> pl.Expr:
    return pl.col(column).dt.convert_time_zone("UTC")


def _d(value: float) -> Decimal:
    return Decimal(str(value))


def season_of(game_id: pl.Expr) -> pl.Expr:
    """A game id's season, as 20262027: its first four digits are the season's first year."""
    first = game_id // 1_000_000
    return (first * 10_000 + first + 1).cast(pl.Int32)


# The opportunities and both arms' fair values


def quotes(odds: pl.DataFrame, day: date) -> pl.DataFrame:
    """Pinnacle's h2h pair of each game at each of the day's SLOTS (slot, snapshot_utc, game_id,
    home_price, away_price, last_update_utc: the older side's), with its fair home probability
    de-vigged multiplicatively; a pair de-vigging refuses is none (live/predict.devigable)."""
    pairs = (
        odds.filter(
            pl.col("book") == BOOK,
            pl.col("market") == "h2h",
            pl.col("slot").is_in(SLOTS),
            pl.col("game_id").is_not_null(),
            pl.col("snapshot_utc").dt.convert_time_zone("America/New_York").dt.date() == day,
        )
        .group_by("slot", "snapshot_utc", "game_id")
        .agg(
            home_price=pl.col("price_decimal").filter(pl.col("side") == "home").first(),
            away_price=pl.col("price_decimal").filter(pl.col("side") == "away").first(),
            last_update_utc=pl.col("last_update_utc").min(),
            commence_time_utc=pl.col("commence_time_utc").min(),
            listed_home=pl.col("home").first(),
            listed_away=pl.col("away").first(),
        )
        .drop_nulls(["home_price", "away_price"])
    )
    if pairs.is_empty():
        return pairs.with_columns(p_home=pl.lit(None, pl.Float64))
    implied = 1 / pairs["home_price"] + 1 / pairs["away_price"]
    pairs = pairs.filter(implied >= 1)
    fair = fair_probabilities(pairs.select("home_price", "away_price").to_numpy())[:, 0]
    return pairs.with_columns(p_home=pl.Series(fair, dtype=pl.Float64))


def starter_changed(rows: pl.DataFrame, dailyfaceoff: pl.DataFrame) -> pl.Series:
    """Per row (game_date, game_id, home, away, start_utc, decision_snapshot_utc, at_utc): whether
    Daily Faceoff's goalie for either team at its latest poll before the row's at_utc differs
    from its latest before the decision's snapshot, by name, as the edge bids read it
    (live/shadow.py). Each row on its own time, so two rows of a slot never share a poll. False
    when no team is named at both."""
    if rows.is_empty() or dailyfaceoff.is_empty():
        return pl.Series([False] * rows.height, dtype=pl.Boolean)
    keys = ["game_date", "game_id", "row"]
    indexed = rows.with_row_index("row")
    teams = pl.concat(
        [
            indexed.select(*keys, "start_utc", "decision_snapshot_utc", "at_utc", team=pl.col(side))
            for side in ("home", "away")
        ]
    )
    before = _named(
        teams.drop("at_utc").rename({"decision_snapshot_utc": "moment_utc"}), dailyfaceoff, keys
    )
    now = _named(
        teams.drop("decision_snapshot_utc").rename({"at_utc": "moment_utc"}), dailyfaceoff, keys
    )
    known = before.join(now, on=[*keys, "team"], suffix="_now").filter(
        pl.col("name").is_not_null(), pl.col("name_now").is_not_null()
    )
    changed = known.group_by(keys).agg(changed=(pl.col("name") != pl.col("name_now")).any())
    return (
        indexed.select(keys)
        .join(changed, on=keys, how="left")
        .sort("row")["changed"]
        .fill_null(False)
    )


def opportunities(
    ledger: pl.DataFrame,
    odds: pl.DataFrame,
    dailyfaceoff: pl.DataFrame,
    weights: Sequence[float] | None,
    day: date,
) -> pl.DataFrame:
    """Each arm's opportunities of the day, one row per arm, slot, game and team (arm, slot,
    at_utc, game_date, season, game_id, team, home, away, start_utc, p_side, hurdle, doubt, left):
    at each slot's fresh Pinnacle pair of a game in the day's ledger. The hockey arm has the
    predicted games with p_B3 and u, while weights (conservative_blend's a, b_m, b_d for the week)
    are given; from the first slot a game's starter changed since the decision, its rows are left
    (left): no hook, and a resting bid of it is cancelled."""
    slate = ledger.filter(pl.col("game_date") == day).select(
        "game_date",
        "season",
        "game_id",
        "home",
        "away",
        "start_utc",
        "published_utc",
        "status",
        "policy_version",
        "p_b3",
        "u_sd",
        "decision_snapshot_utc",
    )
    # A game without a decision row is market-only, at the odds' own listing (ADR 0046).
    fresh = (
        quotes(odds, day)
        .join(slate, on="game_id", how="left")
        .with_columns(
            game_date=pl.col("game_date").fill_null(day),
            season=pl.col("season").fill_null(season_of(pl.col("game_id"))),
            home=pl.col("home").fill_null(pl.col("listed_home")),
            away=pl.col("away").fill_null(pl.col("listed_away")),
            start_utc=pl.col("start_utc").fill_null(_utc("commence_time_utc")),
        )
        .with_columns(
            at_utc=pl.max_horizontal(_utc("snapshot_utc"), _utc("published_utc")),
        )
        .filter(
            pl.col("snapshot_utc") - pl.col("last_update_utc") <= MAX_QUOTE_AGE,
            pl.col("at_utc") - pl.col("last_update_utc") <= MAX_PUBLISHED_AGE,
            pl.col("at_utc") < pl.col("start_utc"),
            pl.col("at_utc") < pl.col("commence_time_utc"),
        )
        .sort("at_utc", "game_id")
    )
    hockey = fresh.filter(
        pl.lit(weights is not None),
        pl.col("policy_version") == challengers.TRAINS_ON,
        pl.col("status") == PREDICTED,
        pl.col("p_b3").is_not_null(),
        pl.col("u_sd").is_not_null(),
        pl.col("decision_snapshot_utc").is_not_null(),
    )
    if hockey.height and weights is not None:
        a, b_m, b_d = weights
        market = logit(hockey["p_home"].to_numpy())
        p = sigmoid(a + b_m * market + b_d * (logit(hockey["p_b3"].to_numpy()) - market))
        changed = starter_changed(hockey, dailyfaceoff)
        hockey = (
            hockey.with_columns(p_home=pl.Series(p, dtype=pl.Float64), changed=changed)
            .sort("at_utc", maintain_order=True)
            .with_columns(left=pl.col("changed").cum_max().over("game_id"))
        )
    else:
        hockey = hockey.with_columns(left=pl.lit(False))
    doubt = pl.when(pl.col("u_sd") > 0).then(pl.col("u_sd")).otherwise(0.0)
    arms = [
        fresh.with_columns(arm=pl.lit(MARKET), doubt=pl.lit(0.0), left=pl.lit(False)),
        hockey.with_columns(arm=pl.lit(HOCKEY), doubt=doubt),
    ]
    rows = []
    for frame in arms:
        if frame.is_empty():
            continue
        for side in ("home", "away"):
            p_side = pl.col("p_home") if side == "home" else 1 - pl.col("p_home")
            rows.append(
                frame.select(
                    "arm",
                    "slot",
                    "at_utc",
                    "game_date",
                    "season",
                    "game_id",
                    "home",
                    "away",
                    "start_utc",
                    "doubt",
                    "left",
                    team=pl.col(side),
                    p_side=p_side,
                    hurdle=POLICY.min_ev + POLICY.ev_per_sd * pl.col("doubt"),
                )
            )
    columns = [
        "arm",
        "slot",
        "at_utc",
        "game_date",
        "season",
        "game_id",
        "home",
        "away",
        "start_utc",
        "team",
        "p_side",
        "hurdle",
        "doubt",
        "left",
    ]
    if not rows:
        return pl.DataFrame(
            schema={
                **{k: OPPORTUNITY_SCHEMA[k] for k in OPPORTUNITY_SCHEMA if k != "action"},
                "home": pl.String,
                "away": pl.String,
                "doubt": pl.Float64,
                "left": pl.Boolean,
            }
        ).select(columns)
    return pl.concat([r.select(columns) for r in rows]).sort(
        "at_utc", "game_id", "team", "arm", maintain_order=True
    )


# Kalshi's books, trades and tops of the day


@dataclass(frozen=True)
class Book:
    """A market's Yes asks, cheapest first, and its Yes bids by price, as observed then."""

    observed_utc: datetime
    asks: tuple[tuple[Decimal, Decimal], ...]
    bids: Mapping[Decimal, Decimal]

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0][0] if self.asks else None


@dataclass(frozen=True)
class Market:
    """The day's logs of one team's market: its books by observation, its trades and its tops."""

    books: tuple[Book, ...]
    trades: tuple[tuple[datetime, Decimal, Decimal], ...]
    asks: tuple[tuple[datetime, Decimal], ...]

    def book(self, at: datetime) -> Book | None:
        """The newest book observed at or before at, at most MAX_BOOK_AGE old."""
        found = None
        for book in self.books:
            if book.observed_utc > at:
                break
            found = book
        if found is None or at - found.observed_utc > MAX_BOOK_AGE:
            return None
        return found


EMPTY = Market((), (), ())


def _price(value: float) -> Decimal:
    return _d(round(value, 4))


def markets(depth: pl.DataFrame, trades: pl.DataFrame, tops: pl.DataFrame) -> dict[Any, Market]:
    """The day's logs by (game_id, team): a book's Yes asks are the No bids turned over (a No bid
    at q sells Yes at 1 - q)."""
    found: dict[Any, dict[str, list[Any]]] = {}

    def entry(key: Any) -> dict[str, list[Any]]:
        return found.setdefault(key, {"books": [], "trades": [], "asks": []})

    for (game_id, team, observed), levels in depth.sort("observed_utc").group_by(
        "game_id", "team", "observed_utc", maintain_order=True
    ):
        asks = sorted(
            (Decimal(1) - _price(r["price"]), _d(r["quantity"]))
            for r in levels.filter(pl.col("side") == "no").iter_rows(named=True)
        )
        bids = {
            _price(r["price"]): _d(r["quantity"])
            for r in levels.filter(pl.col("side") == "yes").iter_rows(named=True)
        }
        entry((game_id, team))["books"].append(
            Book(observed.astimezone(UTC), tuple(asks), bids)  # type: ignore[union-attr]
        )
    for r in trades.sort("created_utc").iter_rows(named=True):
        entry((r["game_id"], r["team"]))["trades"].append(
            (r["created_utc"].astimezone(UTC), _price(r["yes_price"]), _d(r["count"]))
        )
    for r in (
        tops.filter(pl.col("yes_ask").is_not_null()).sort("observed_utc").iter_rows(named=True)
    ):
        entry((r["game_id"], r["team"]))["asks"].append(
            (r["observed_utc"].astimezone(UTC), _price(r["yes_ask"]))
        )
    return {
        key: Market(tuple(v["books"]), tuple(v["trades"]), tuple(v["asks"]))
        for key, v in found.items()
    }


# One arm's day


def kelly(p: float, doubt: float) -> Callable[[float], float]:
    """The stake in euros ADR 0028 allows at a decimal price: quarter Kelly divided by (1 + u⁺),
    at most max_bet of the bankroll; 0 at a price whose EV is below the hurdle's floor."""

    def allowed(o: float) -> float:
        ev = p * o - 1
        if o <= 1 or ev <= 0:
            return 0.0
        return BANKROLL * min(POLICY.max_bet, POLICY.kelly * ev / (o - 1) / (1 + doubt))

    return allowed


def capped(allowed: Callable[[float], float], room: float) -> Callable[[float], float]:
    return lambda o: max(0.0, min(allowed(o), room))


def take(
    asks: Sequence[tuple[Decimal, Decimal]],
    p: float,
    hurdle: float,
    allowed: Callable[[float], float],
    rules: Exchange,
) -> tuple[Decimal, Decimal, list[tuple[Decimal, Decimal, Decimal]]]:
    """The taker part from the asks, cheapest first: the most contracts whose cost, each level at
    the taker fee rounded up, clears the hurdle at its effective price and is within the stake
    allowed there, plus a half cent (exchange.sized's margin). Its contracts, cost in USD, and
    each level's (price, contracts, cost)."""
    step, rate = _d(rules.step), _d(rules.usd_per_eur)
    contracts, spent = Decimal(0), Decimal(0)
    levels: list[tuple[Decimal, Decimal, Decimal]] = []

    def ok(c: Decimal, k: Decimal) -> bool:
        return (
            k > 0
            and p * float(c / k) - 1 >= hurdle
            and float(k / rate) + ex.HALF_CENT <= allowed(float(c / k)) + 1e-12
        )

    for price, quantity in asks:
        top = int((quantity / step).to_integral_value(ROUND_FLOOR))
        low, high = 0, top
        while low < high:
            mid = (low + high + 1) // 2
            q = step * mid
            if ok(contracts + q, spent + ex.cost(q, price, rules)):
                low = mid
            else:
                high = mid - 1
        if low == 0:
            break
        q = step * low
        k = ex.cost(q, price, rules)
        contracts, spent = contracts + q, spent + k
        levels.append((price, q, k))
        if low < top:
            break
    return contracts, spent, levels


@dataclass
class Bid:
    """A resting maker bid: its price, contracts left and filled, the cost of its fills, when its
    current price was posted, the contracts ahead of it then still to trade, and its expiry."""

    game_id: int
    team: str
    p_side: float
    hurdle: float
    price: Decimal
    left: Decimal
    posted_utc: datetime
    queue: Decimal
    expires_utc: datetime
    placed_utc: datetime
    since: datetime = field(init=False)
    filled_cost: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        self.since = self.posted_utc

    def planned(self, rules: Exchange) -> Decimal:
        """Its cost if what is left fills, with what filled: what the budget counts."""
        return self.filled_cost + ex.cost(self.left, self.price, rules, maker=True)


def advance(
    bid: Bid, until: datetime, market: Market, rules: Exchange
) -> list[tuple[datetime, Decimal, Decimal]]:
    """The bid's fills after its last advance up to until (and its expiry): each trade at or below
    its price first takes the contracts ahead of it, then fills it; an ask at or below its price
    in a top of book fills what is left. Each fill is (time, contracts, cost at the maker fee)."""
    end = min(until, bid.expires_utc)
    out = []
    events: list[tuple[datetime, int, Decimal]] = [
        (t, 0, count)
        for t, price, count in market.trades
        if bid.since < t <= end and price <= bid.price
    ]
    events += [
        (t, 1, Decimal(0)) for t, ask in market.asks if bid.since < t <= end and ask <= bid.price
    ]
    for t, kind, count in sorted(events):
        if bid.left <= 0:
            break
        if kind == 1:
            q = bid.left
        else:
            ahead = min(bid.queue, count)
            bid.queue -= ahead
            q = min(bid.left, count - ahead)
        if q <= 0:
            continue
        k = ex.cost(q, bid.price, rules, maker=True)
        bid.left -= q
        bid.filled_cost += k
        out.append((t, q, k))
    bid.since = max(bid.since, end)
    return out


@dataclass
class ArmDay:
    """One arm's hooks of a day: its exposure in euros by game and in all, the team held on each
    game, its resting bids, its fills and what each opportunity did."""

    arm: str
    rules: Exchange
    day_exposure: float = 0.0
    games: dict[int, float] = field(default_factory=dict)
    held: dict[int, str] = field(default_factory=dict)
    bids: dict[tuple[int, str], Bid] = field(default_factory=dict)
    fills: list[dict[str, Any]] = field(default_factory=list)
    actions: list[tuple[Mapping[str, Any], str]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def count(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    def room(self, game_id: int) -> float:
        return max(
            0.0,
            min(
                POLICY.max_bet * BANKROLL - self.games.get(game_id, 0.0),
                POLICY.max_day * BANKROLL - self.day_exposure,
            ),
        )

    def spend(self, game_id: int, usd: Decimal) -> None:
        eur = float(usd / _d(self.rules.usd_per_eur))
        self.games[game_id] = self.games.get(game_id, 0.0) + eur
        self.day_exposure += eur

    def record(
        self, row: Mapping[str, Any], kind: str, placed: datetime, fills: Iterable[Any]
    ) -> None:
        for at, contracts, cost in fills:
            self.fills.append(
                {
                    "arm": self.arm,
                    "game_date": row["game_date"],
                    "season": row["season"],
                    "game_id": row["game_id"],
                    "team": row["team"],
                    "start_utc": row["start_utc"],
                    "placed_utc": placed,
                    "fill_utc": at,
                    "kind": kind,
                    "contracts": float(contracts),
                    "cost_usd": float(cost),
                }
            )


def _ev_at_ask(row: Mapping[str, Any], market: Market | None, rules: Exchange) -> float:
    book = market.book(row["at_utc"]) if market is not None else None
    if book is None or book.best_ask is None:
        return -1.0
    unit = ex.cost(Decimal(1), book.best_ask, rules)
    return row["p_side"] / float(unit) - 1


def run_arm(
    arm: str,
    rows: pl.DataFrame,
    logs: Mapping[Any, Market],
    rules: Exchange,
) -> ArmDay:
    """One arm's day: slot by slot in time, each resting bid advanced and re-valued, then each
    opportunity's taker part and maker bid, highest expected return at the best ask first."""
    state = ArmDay(arm, rules)
    by_slot = rows.sort("at_utc").partition_by("slot", "at_utc", maintain_order=True)
    rows_by_key = {(r["game_id"], r["team"]): r for r in rows.iter_rows(named=True)}
    for slot_rows in by_slot:
        at = slot_rows["at_utc"][0]
        here = {(r["game_id"], r["team"]): r for r in slot_rows.iter_rows(named=True)}
        # the resting bids first: their fills to now, then the slot's value
        for key, bid in list(state.bids.items()):
            market = logs.get(key, EMPTY)
            filled = advance(bid, at, market, rules)
            state.record(rows_by_key[key], MAKER, bid.placed_utc, filled)
            if bid.left <= 0:
                del state.bids[key]
                state.count("filled")
                continue
            if at >= bid.expires_utc:
                _release(state, key, bid, "expired")
                continue
            row = here.get(key)
            if row is None:
                continue  # no fresh quote at this slot: it rests as it is
            if row["left"]:
                _release(state, key, bid, "cancelled")
                continue
            lower = rv.lowered(row["p_side"], row["hurdle"], bid.price, bid.left, rules)
            if lower is None:
                _release(state, key, bid, "cancelled")
            elif lower < bid.price:
                before = bid.planned(rules)
                book = market.book(at)
                bid.price = lower
                bid.queue = book.bids.get(lower, Decimal(0)) if book is not None else Decimal(0)
                bid.posted_utc = at
                bid.since = at
                state.spend(key[0], bid.planned(rules) - before)
                state.count("lowered")
        ordered = sorted(
            slot_rows.iter_rows(named=True),
            key=lambda r: -_ev_at_ask(r, logs.get((r["game_id"], r["team"])), rules),
        )
        for row in ordered:
            if row["left"]:
                continue
            key = (row["game_id"], row["team"])
            held = state.held.get(row["game_id"])
            if held is not None and held != row["team"]:
                state.actions.append((row, OTHER_TEAM))
                continue
            market = logs.get(key)
            book = market.book(at) if market is not None else None
            if book is None:
                state.actions.append((row, NO_BOOK))
                continue
            room = state.room(row["game_id"])
            if room <= 0:
                state.actions.append((row, HELD))
                continue
            allowed = capped(kelly(row["p_side"], row["doubt"]), room)
            took, spent, levels = take(book.asks, row["p_side"], row["hurdle"], allowed, rules)
            if took > 0:
                state.spend(row["game_id"], spent)
                state.held[row["game_id"]] = row["team"]
                state.record(row, TAKER, at, [(at, q, k) for _, q, k in levels])
            posted = False
            if key not in state.bids:
                posted = _post(state, row, book, allowed, (took, spent), at)
            action = (
                BOTH if took > 0 and posted else TOOK if took > 0 else POSTED if posted else NONE
            )
            state.actions.append((row, action))
    for key, bid in list(state.bids.items()):
        market = logs.get(key, EMPTY)
        filled = advance(bid, bid.expires_utc, market, rules)
        state.record(rows_by_key[key], MAKER, bid.placed_utc, filled)
        state.count("expired" if bid.left > 0 else "filled")
    state.bids.clear()
    return state


def _release(state: ArmDay, key: tuple[int, str], bid: Bid, why: str) -> None:
    """A bid cancelled or expired: what it left unfilled no longer counts against the budget."""
    state.spend(key[0], bid.filled_cost - bid.planned(state.rules))
    bid.left = Decimal(0)
    del state.bids[key]
    state.count(why)


def _post(
    state: ArmDay,
    row: Mapping[str, Any],
    book: Book,
    allowed: Callable[[float], float],
    prior: tuple[Decimal, Decimal],
    at: datetime,
) -> bool:
    """The maker bid after the taker part, as a real order's (exchange.order and exchange.rest),
    kept a cent below the best ask. Whether one rests."""
    rules = state.rules
    planned = ex.order(row["p_side"], row["hurdle"], allowed, rules, maker=True)
    if planned is None:
        return False
    price = planned.price
    if book.best_ask is not None and price >= book.best_ask:
        price = book.best_ask - CENT
    if price < CENT:
        return False
    took, spent = prior
    if took == 0 and price == planned.price:
        contracts = planned.contracts
    else:
        budget = planned.cost - spent
        contracts = ex.rest(price, budget, prior, row["p_side"], row["hurdle"], allowed, rules)
    if contracts < _d(rules.step):
        return False
    bid = Bid(
        game_id=row["game_id"],
        team=row["team"],
        p_side=row["p_side"],
        hurdle=row["hurdle"],
        price=price,
        left=contracts,
        posted_utc=at,
        queue=book.bids.get(price, Decimal(0)),
        expires_utc=row["start_utc"] - rules.expire_before,
        placed_utc=at,
    )
    state.bids[(row["game_id"], row["team"])] = bid
    state.spend(row["game_id"], bid.planned(rules))
    state.held[row["game_id"]] = row["team"]
    state.count("posted")
    return True


# The day, and what is kept of it


@dataclass(frozen=True)
class DayResult:
    """A day's opportunities with what each did, its fills, and its counts per arm."""

    opportunities: pl.DataFrame
    fills: pl.DataFrame
    counts: dict[str, Any]


def run_day(
    day: date,
    ledger: pl.DataFrame,
    odds: pl.DataFrame,
    depth: pl.DataFrame,
    trades: pl.DataFrame,
    tops: pl.DataFrame,
    dailyfaceoff: pl.DataFrame,
    weights: Sequence[float] | None,
    rules: Exchange,
) -> DayResult:
    """Both arms' day from the day's ledger, odds snapshots, Kalshi logs and Daily Faceoff polls,
    with conservative_blend's weights for the week (None while it is unfitted)."""
    rows = opportunities(ledger, odds, dailyfaceoff, weights, day)
    logs = markets(depth, trades, tops)
    found, acted, counts = [], [], {}
    for arm in ARMS:
        of_arm = rows.filter(pl.col("arm") == arm)
        state = run_arm(arm, of_arm, logs, rules)
        acted.append(
            pl.DataFrame(
                [
                    {k: r[k] for k in OPPORTUNITY_SCHEMA if k != "action"} | {"action": a}
                    for r, a in state.actions
                ],
                schema=OPPORTUNITY_SCHEMA,
            )
        )
        found += state.fills
        actions = [a for _, a in state.actions]
        counts[arm] = {
            "opportunities": len(actions),
            "actions": {a: actions.count(a) for a in sorted(set(actions))},
            "bids": state.counts,
            "fills": len(state.fills),
        }
    fills = pl.DataFrame(found, schema=lf.FILL_SCHEMA | {"arm": pl.String})
    fills = adverse(fills, ledger, odds, dailyfaceoff, day)
    counts["fitted"] = weights is not None
    counts["watch_logs"] = depth.height > 0
    return DayResult(
        pl.concat(acted) if acted else pl.DataFrame(schema=OPPORTUNITY_SCHEMA), fills, counts
    )


def adverse(
    found: pl.DataFrame,
    ledger: pl.DataFrame,
    odds: pl.DataFrame,
    dailyfaceoff: pl.DataFrame,
    day: date,
) -> pl.DataFrame:
    """Each fill with starter_changed (Daily Faceoff's goalie for either team at the fill differs
    from at the hook's placement) and moved_against (Pinnacle's fair price for the team at its
    newest pair before the fill is below its pair before the placement); null when unknown."""
    if found.is_empty():
        return found.with_columns(
            starter_changed=pl.lit(None, pl.Boolean), moved_against=pl.lit(None, pl.Boolean)
        ).select(list(FILL_SCHEMA))
    teams = ledger.filter(pl.col("game_date") == day).select("game_id", "home", "away")
    rows = found.with_row_index("fill").join(teams, on="game_id", how="left")
    moments = rows.select(
        "game_date",
        "game_id",
        "home",
        "away",
        "start_utc",
        decision_snapshot_utc="placed_utc",
        at_utc="fill_utc",
    )
    changed = starter_changed(moments, dailyfaceoff)
    pairs = quotes(odds, day).select("game_id", "snapshot_utc", "p_home").sort("snapshot_utc")

    def fair_before(column: str) -> pl.Series:
        # sorted by time within each game, as the join needs; it can't check that across groups
        joined = rows.sort("game_id", column).join_asof(
            pairs.sort("game_id", "snapshot_utc"),
            left_on=column,
            right_on="snapshot_utc",
            by="game_id",
            strategy="backward",
            check_sortedness=False,
        )
        joined = joined.sort("fill")
        side = (
            pl.when(pl.col("team") == pl.col("home"))
            .then(pl.col("p_home"))
            .otherwise(1 - pl.col("p_home"))
        )
        return joined.select(side.alias("p"))["p"]

    if pairs.is_empty():
        moved = pl.Series([None] * rows.height, dtype=pl.Boolean)
    else:
        placed, filled = fair_before("placed_utc"), fair_before("fill_utc")
        moved = (filled < placed).cast(pl.Boolean)
    return rows.with_columns(starter_changed=changed, moved_against=moved).select(list(FILL_SCHEMA))


def day_keys(day: date) -> tuple[str, str, str]:
    """Where a day's counts, fills and opportunities are kept, each written once."""
    base = f"{PREFIX}/days/{day.isoformat()}"
    return f"{base}.json", f"{base}-fills.parquet", f"{base}-opportunities.parquet"


def stored_days(store: lb.Store) -> list[date]:
    """The days whose counts are kept: the last file a day writes."""
    found = []
    for key in store.keys(f"{PREFIX}/days"):
        name = key.rsplit("/", 1)[-1]
        if name.endswith(".json"):
            found.append(date.fromisoformat(name.removesuffix(".json")))
    return sorted(found)


def keep(
    store: lb.Store,
    day: date,
    result: DayResult,
    code_version: str,
    now: datetime,
    fit: Mapping[str, Any] | None = None,
) -> None:
    """A day's files, written once: its fills and opportunities, then its counts with the hockey
    arm's fit (its week, training games and train_cutoff), the last."""
    counts_key, fills_key, opportunities_key = day_keys(day)
    lb.put_once(store, fills_key, lb.parquet(result.fills))
    lb.put_once(store, opportunities_key, lb.parquet(result.opportunities))
    meta = {
        "shadow": SHADOW,
        "version": VERSION,
        "adr": ADR,
        "day": day.isoformat(),
        "computed_utc": now.isoformat(),
        "code_version": code_version,
        "counts": result.counts,
        "fit": dict(fit) if fit is not None else None,
    }
    lb.put_once(store, counts_key, json.dumps(meta, sort_keys=True).encode())


def kept(store: lb.Store) -> tuple[pl.DataFrame, pl.DataFrame, list[dict[str, Any]]]:
    """Every kept day's fills, opportunities and counts."""
    fills, opportunities, metas = [], [], []
    for day in stored_days(store):
        counts_key, fills_key, opportunities_key = day_keys(day)
        metas.append(json.loads(store.get(counts_key)))
        fills.append(pl.read_parquet(io.BytesIO(store.get(fills_key))))
        opportunities.append(pl.read_parquet(io.BytesIO(store.get(opportunities_key))))
    return (
        pl.concat(fills) if fills else pl.DataFrame(schema=FILL_SCHEMA),
        pl.concat(opportunities) if opportunities else pl.DataFrame(schema=OPPORTUNITY_SCHEMA),
        metas,
    )


# The measures


def valued(rows: pl.DataFrame, rate: float) -> pl.DataFrame:
    """Each arm's valued bids: per arm, game, team and kind, the fills observed before its game's
    proxy, with contracts, cost, CLV at the matched price and the expected profit in euros."""
    keys = ["arm", "game_date", "season", "game_id", "team", "kind"]
    return (
        rows.filter(pl.col("reference") == lf.VALUED)
        .group_by(keys, maintain_order=True)
        .agg(pl.col("contracts").sum(), pl.col("cost_usd").sum(), p_close=pl.col("p_close").first())
        .with_columns(
            clv=pl.col("p_close") * pl.col("contracts") / pl.col("cost_usd") - 1,
            profit_eur=(pl.col("p_close") * pl.col("contracts") - pl.col("cost_usd")) / rate,
        )
        .sort(keys)
    )


def paired(
    bids: pl.DataFrame, opportunities: pl.DataFrame, fitted_days: Iterable[date]
) -> pl.DataFrame:
    """Per opportunity (game and team) of a day both arms ran, the hockey arm's expected profit
    captured less the market arm's, each 0 without a valued fill."""
    days = set(fitted_days)
    keys = ["game_date", "season", "game_id", "team"]
    both = (
        opportunities.filter(pl.col("game_date").is_in(sorted(days)))
        .group_by(keys)
        .agg(arms=pl.col("arm").n_unique())
        .filter(pl.col("arms") == len(ARMS))
        .select(keys)
    )
    profit = bids.group_by("arm", *keys).agg(pl.col("profit_eur").sum())
    out = both
    for arm in ARMS:
        out = out.join(
            profit.filter(pl.col("arm") == arm).select(*keys, pl.col("profit_eur").alias(arm)),
            on=keys,
            how="left",
        )
    return out.with_columns(
        difference=pl.col(HOCKEY).fill_null(0.0) - pl.col(MARKET).fill_null(0.0)
    ).sort(keys)


def _share(frame: pl.DataFrame, column: str) -> dict[str, int]:
    known = frame.filter(pl.col(column).is_not_null())
    return {"fills": known.height, "of_them": known.filter(pl.col(column)).height}


def summary(
    rows: pl.DataFrame, opportunities: pl.DataFrame, metas: Sequence[Mapping[str, Any]], rate: float
) -> dict[str, Any]:
    """The private figures over every kept day (rows: the fills with their reference status,
    fills.referenced)."""
    bids = valued(rows, rate)
    fitted = [date.fromisoformat(m["day"]) for m in metas if m["counts"].get("fitted")]
    result: dict[str, Any] = {
        "shadow": SHADOW,
        "version": VERSION,
        "adr": ADR,
        "days": len(metas),
        "days_with_watch_logs": sum(1 for m in metas if m["counts"].get("watch_logs")),
        "days_fitted": len(fitted),
    }
    for arm in ARMS:
        of_arm = rows.filter(pl.col("arm") == arm)
        figures: dict[str, Any] = {
            "opportunities": opportunities.filter(pl.col("arm") == arm).height,
        }
        for kind in KINDS:
            of_kind = of_arm.filter(pl.col("kind") == kind)
            kept_bids = bids.filter(pl.col("arm") == arm, pl.col("kind") == kind)
            figures[kind] = {
                "fills": {
                    s: of_kind.filter(pl.col("reference") == s).height for s in lf.REFERENCES
                },
                "bids": kept_bids.height,
                "clv_per_bid": lr.estimate(kept_bids, "clv") if kept_bids.height else None,
                "profit_eur_per_bid": (
                    lr.estimate(kept_bids, "profit_eur") if kept_bids.height else None
                ),
                "profit_eur": float(kept_bids["profit_eur"].sum()) if kept_bids.height else 0.0,
            }
        makers = of_arm.filter(pl.col("kind") == MAKER)
        figures["adverse"] = {
            "starter_changed": _share(makers, "starter_changed"),
            "moved_against": _share(makers, "moved_against"),
        }
        result[arm] = figures
    pairs = paired(bids, opportunities, fitted)
    result["paired_difference_eur"] = lr.estimate(pairs, "difference") if pairs.height else None
    return result


def count_line(metas: Sequence[Mapping[str, Any]], computed: int) -> str:
    """The log's line: counts only, never a price, a cost or a figure (#280)."""
    with_logs = sum(1 for m in metas if m["counts"].get("watch_logs"))
    fitted = sum(1 for m in metas if m["counts"].get("fitted"))
    fills = sum(m["counts"].get(arm, {}).get("fills", 0) for m in metas for arm in ARMS)
    return (
        f"kalshi shadow: {computed} days computed now, {len(metas)} kept "
        f"({with_logs} with watch logs, {fitted} with the hockey arm fitted), {fills} fills"
    )


def report_key(as_of: datetime) -> str:
    return f"{PREFIX}/reports/{as_of:%Y-%m-%dT%H%M%SZ}.json"


def week_fit(ledger: pl.DataFrame, games: pl.DataFrame, day: date) -> dict[str, Any]:
    """conservative_blend's fit for the day's week (ADR 0041, live/challengers.py): its MAP fit on
    the earlier weeks' v2 decisions and results public before the week's fold start, with the
    week, its fold start, its training games and their last result (train_cutoff); without
    weights while fewer than MINIMUM_GAMES games train it."""
    rows = challengers.training_rows(ledger, games, day)
    fit: dict[str, Any] = {
        "challenger": "conservative_blend",
        "week": challengers.week_of(day).isoformat(),
        "fold_start_utc": challengers.fold_start(day).isoformat(),
        "games": rows.height,
        "minimum": challengers.MINIMUM_GAMES,
    }
    if rows.height < challengers.MINIMUM_GAMES:
        return fit
    weights, _ = challengers.conservative_fit(rows["home_win"], rows["p_b0"], rows["p_b3"])
    fit["weights"] = [float(w) for w in np.asarray(weights)]
    fit["train_cutoff"] = rows["result_utc"].max().isoformat()  # type: ignore[union-attr]
    return fit


def weights_of(fit: Mapping[str, Any]) -> tuple[float, float, float] | None:
    """A fit's a, b_m and b_d, or None while the week is unfitted."""
    if "weights" not in fit:
        return None
    a, b_m, b_d = fit["weights"]
    return a, b_m, b_d


def due(today: date, done: Iterable[date]) -> list[date]:
    """The game dates from FIRST_DAY before today (US Eastern) not kept yet: a day is read once
    all its games started, the night after."""
    kept_days = set(done)
    found, day = [], FIRST_DAY
    while day < today:
        if day not in kept_days:
            found.append(day)
        day += timedelta(days=1)
    return found
