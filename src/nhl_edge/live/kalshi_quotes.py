"""Shadow kalshi_quotes (#358 step 1): two-sided quotes on Kalshi's game-winner markets, from three
fair values, a paper simulator on the watch's logs. No paper bet, real bet or policy reads it.

The owner decided on 2026-10-30 to pursue market making on Kalshi with our own model as the fair
value, posted early. Its first risk is adverse selection: live, where the model disagreed with the
market, the market was right, so a quote at the model's price may fill mostly where the model is
wrong. Each fair value is run apart, at each half-spread, so the readings show whether quoting both
sides earns the spread or loses it. The rules below were fixed on RULES_FIXED, before FIRST_DAY,
the first day it reads; the readings are design evidence for the ADR that comes before any order
(#358 step 4).

**Fair values (FAIRS),** each at a moment from inputs known by then (hard rule 1):
- **model:** the blend. The morning view's p_blend (morning_predictions, ADR 0052) from its
  publication, then the decision's (paper_ledger) from its publication.
- **anchored:** the model shrunk toward Kalshi's own mid in log-odds, by ANCHOR_WEIGHT. The mid is
  the team's market's latest top of book (kalshi_tops), observed by then and at most MAX_MID_AGE
  old, with both a bid and an ask.
- **pinnacle:** Pinnacle's latest h2h pair before the moment, fresh at its snapshot, de-vigged
  multiplicatively (market/devig.py, hard rule 3): a reference only.
- **forecast:** the close forecaster's p_close_hat (close_forecasts, ADR 0054), version 1's,
  from its publication, on game dates from FORECAST_FIRST_DAY, the first after its registration:
  its rules were fixed before then, and the other fair values' are unchanged.
- **forecast2:** the same from version 2 (ADR 0056), with schedule density, on game dates from
  FORECAST2_FIRST_DAY, the first after its registration.

**Quotes.** At each step (a top of book of either team's market, or a fair value's update), each
team's Yes market gets a post-only bid of SIZE contracts at the fair value less the half-spread h,
rounded down to the cent (the fair value first rounded to FAIR_PLACES decimals from ROUNDED_FROM,
#376), and at least a cent below the market's best ask; none below a cent. Both
teams' Yes bought is the two-sided quote: exactly one team wins on the full game (hard rule 2). A
bid is replaced when its price would move by a cent or more, and after it fills.
- **Limits:** a team's bid is sized so that our contracts on it, less those on the other team,
  never pass NET_CAP. No bid is posted that would take the day's filled and resting cost past
  DAY_CAP.
- **Pulls:** a game's bids are cancelled when Daily Faceoff names another starter for either team
  (STARTER), or when a team's mid moves PULL_MOVE or more since its bid's posting (MOVE). The game
  then waits for its fair value's source to update after the pull: the model's next publication
  for model, pinnacle's next snapshot for pinnacle, and for anchored the mid after a MOVE, the
  model after a STARTER. A starter change holds the game so even with no bid resting.
- **The end:** nothing is quoted from STOP_BEFORE the start.

**Fills,** kalshi_shadow.advance's rule (ADR 0046): a trade at or below the bid takes the depth
ahead of it at posting first, then fills it; an ask at or below it in a later top fills the rest.
Partial fills count. An upper bound on fills, so on the adverse ones too.

**Measures,** per variant (fair value and half-spread), per contract: the spread captured (the fair
value at posting less the price); the markouts, Kalshi's mid MARKOUTS after the fill and its last
mid before the start, and Pinnacle's closing proxy observed after the fill (live/fills.py), each
less the price; and the profit at settlement, the result on the full game less the cost with the
maker fee. Beside them: the pairs (contracts bought on both teams of a game, whose payout is fixed)
and the largest inventory. Each through the live report's estimate (a weekly block bootstrap
interval, counts only under 4 weeks, hard rule 7).

**Private.** It reads Kalshi's books and trades (#280): every figure goes to the record's store
under PREFIX, and the log prints counts only. The owner chose on 2026-11-05 to see the figures on
the dashboard (#398): with --supabase, day_figures' sums per game date and variant and each
night's summary go to owner-only tables. No fill's price, ticker, time or trade goes there.

**Hold rules (#385),** a measurement beside the variants above, kept apart under HOLDS_PREFIX so
their record never changes: the model fair value at each half-spread, under HOLDS, the rules for
how long a MOVE pull holds a game. Fixed on HOLDS_FIXED, before any figure under them was read,
and scored from HOLDS_FIRST_DAY, the day after: the days before led to them (#385). A STARTER
pull holds until the fair value updates under every rule.
- **wait:** the rule above: until the fair value's source updates.
- **cool:** until COOL_OFF after the pull, or the update if sooner.
- **return:** until the mid of the market that moved is within PULL_MOVE of its mid when the
  pulled bid was posted, or the update if sooner.
- **trust:** no MOVE pull at all; the quote stays at our price (the owner's option, #385).
Beside the measures above, each variant counts the minutes it had a bid resting, in all and in
each game's LATE before its stop. The primary measure, at PRIMARY_HALF_SPREAD, is each rule's
profit at settlement per game less wait's on the same games, with Holm's adjustment over the three
rules once there are MIN_WEEKS weeks.
"""

import io
import json
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

import numpy as np
import polars as pl

from nhl_edge.backtest import metrics
from nhl_edge.backtest.walk_forward import outcomes
from nhl_edge.ingest import kalshi_watch as kw
from nhl_edge.lineup import confirmed
from nhl_edge.live import bundle as lb
from nhl_edge.live import close_forecast as cf
from nhl_edge.live import exchange as ex
from nhl_edge.live import fills as lf
from nhl_edge.live import kalshi_shadow as ks
from nhl_edge.live import report as lr
from nhl_edge.live.predict import BOOK, MAX_QUOTE_AGE, PREDICTED
from nhl_edge.live.real import Exchange
from nhl_edge.market.devig import fair_probabilities
from nhl_edge.market.recalibration import logit, sigmoid

SHADOW, VERSION = "kalshi_quotes", "1"
PREFIX = f"real/shadow/{SHADOW}/{VERSION}"
# The rules below were fixed on RULES_FIXED, after the owner's choice of the half-spreads, which
# was informed by the watch's tops of 2026-10-28 to 10-30. The first game date it reads is the next
# one, so no day it scores informed a rule.
RULES_FIXED = date(2026, 10, 30)
FIRST_DAY = date(2026, 10, 31)
MODEL, ANCHORED, PINNACLE, FORECAST = "model", "anchored", "pinnacle", "forecast"
FORECAST2 = "forecast2"
FAIRS = (MODEL, ANCHORED, PINNACLE, FORECAST, FORECAST2)
FORECAST_FIRST_DAY = cf.REGISTERED_ON + timedelta(days=1)
FORECAST2_FIRST_DAY = cf.REGISTERED_ON_2 + timedelta(days=1)
# Each close forecast fair value's version.
FORECASTS = {FORECAST: cf.VERSION, FORECAST2: cf.VERSION_2}
# Kalshi's NHL winner markets quote a cent wide nearly always (measured 2026-10-30 on the watch's
# tops): at 0 a quote joins the best bid when the fair value allows, at 1 and 2 it sits behind it.
# The owner's choice of 2026-10-30, over 2, 4 and 6 cents, which a cent-wide market never fills.
HALF_SPREADS = (Decimal("0"), Decimal("0.01"), Decimal("0.02"))
SIZE = Decimal(10)
NET_CAP = Decimal(30)
DAY_CAP = Decimal(100)
STOP_BEFORE = timedelta(minutes=10)
PULL_MOVE = Decimal("0.03")
ANCHOR_WEIGHT = 0.5
MAX_MID_AGE = timedelta(minutes=5)
# A market further than the watch's NEAR from its start has its top every 15 minutes and its book
# every 30 (#319): an older top or book still counts for it.
FAR_TOP_AGE = timedelta(minutes=20)
FAR_BOOK_AGE = timedelta(minutes=35)
MARKOUTS = (timedelta(minutes=5), timedelta(minutes=30))
STARTER, MOVE = "starter", "move"
CENT = Decimal("0.01")
# #376, the owner's go-ahead of 2026-11-04: a float fair value just below a cent floored a cent
# too low. From ROUNDED_FROM's games on, the fair value is rounded to FAIR_PLACES decimals
# first. Earlier days keep the floor they were kept with, so a replay gives the record's figures.
ROUNDED_FROM = date(2026, 11, 5)
FAIR_PLACES = 10
# The hold rules after a MOVE pull (#385), fixed on HOLDS_FIXED before any figure under them was
# read. WAIT is the simulator's own rule.
WAIT, COOL, RETURN, TRUST = "wait", "cool", "return", "trust"
HOLDS = (WAIT, COOL, RETURN, TRUST)
HOLDS_FIXED = date(2026, 11, 3)
# The first day the hold rules are scored on: the days before it led to them (#385, the demo of
# 2026-11-02), so a replay of them is a check of the mechanics only, never kept in this record.
HOLDS_FIRST_DAY = HOLDS_FIXED + timedelta(days=1)
HOLDS_VERSION = "1"
HOLDS_PREFIX = f"real/shadow/{SHADOW}/holds/{HOLDS_VERSION}"
COOL_OFF = timedelta(minutes=15)
LATE = timedelta(hours=3)
PRIMARY_HALF_SPREAD = Decimal("0.01")
# The lake tables a run reads.
TABLES = (
    "kalshi_tops",
    "kalshi_depth",
    "kalshi_trades",
    "morning_predictions",
    "close_forecasts",
    "paper_ledger",
    "odds_snapshots",
    "dailyfaceoff_goalies",
    "games",
)
UTC_TYPE = pl.Datetime("us", UTC)
FILL_SCHEMA = {
    "variant": pl.String,
    "fair": pl.String,
    "half_spread": pl.Float64,
    "game_date": pl.Date,
    "season": pl.Int32,
    "game_id": pl.Int64,
    "team": pl.String,
    "start_utc": UTC_TYPE,
    "posted_utc": UTC_TYPE,
    "fill_utc": UTC_TYPE,
    "contracts": pl.Float64,
    "price": pl.Float64,
    "cost_usd": pl.Float64,
    "fair_at_post": pl.Float64,
    "mid_5m": pl.Float64,
    "mid_30m": pl.Float64,
    "mid_last": pl.Float64,
    "p_close": pl.Float64,
    "close_after_fill": pl.Boolean,
    "won": pl.Int8,
}


def variants() -> list[tuple[str, Decimal]]:
    return [(fair, h) for fair in FAIRS for h in HALF_SPREADS]


def variant_name(fair: str, h: Decimal) -> str:
    return f"{fair}-{int(h * 100)}c"


def hold_variants() -> list[tuple[Decimal, str]]:
    """The hold rules' variants (#385): the model at each half-spread under each rule."""
    return [(h, hold) for hold in HOLDS for h in HALF_SPREADS]


def hold_name(h: Decimal, hold: str) -> str:
    return f"{variant_name(MODEL, h)}-{hold}"


@dataclass(frozen=True)
class Game:
    game_id: int
    season: int
    game_date: date
    home: str
    away: str
    start_utc: datetime

    def other(self, team: str) -> str:
        return self.away if team == self.home else self.home

    def side(self, team: str, p_home: float) -> float:
        return p_home if team == self.home else 1 - p_home


def slate_of(ledger: pl.DataFrame) -> list[Game]:
    """The day's games from its ledger rows, soonest first."""
    rows = ledger.unique("game_id", keep="last").sort("start_utc", "game_id")
    return [
        Game(
            r["game_id"],
            r["season"],
            r["game_date"],
            r["home"],
            r["away"],
            r["start_utc"].astimezone(UTC),
        )
        for r in rows.iter_rows(named=True)
    ]


# The fair values' sources


Sources = dict[int, list[tuple[datetime, float]]]


def model_sources(ledger: pl.DataFrame, morning: pl.DataFrame) -> Sources:
    """Each game's blend (home side) by when it was published: the morning view's, then the
    decision's, each only where predicted."""
    found: Sources = {}
    for frame in (morning, ledger):
        predicted = frame.filter(pl.col("status") == PREDICTED, pl.col("p_blend").is_not_null())
        for r in predicted.iter_rows(named=True):
            found.setdefault(r["game_id"], []).append(
                (r["published_utc"].astimezone(UTC), float(r["p_blend"]))
            )
    return {k: sorted(v) for k, v in found.items()}


def forecast_sources(forecasts: pl.DataFrame, fair: str = FORECAST) -> Sources:
    """Each game's close forecast (home side) of the fair value's version (FORECASTS) by when it
    was published, only where forecast and on game dates from the version's first day."""
    version = FORECASTS[fair]
    first = FORECAST2_FIRST_DAY if fair == FORECAST2 else FORECAST_FIRST_DAY
    found: Sources = {}
    forecast = forecasts.filter(
        pl.col("forecast_version") == version,
        pl.col("status") == PREDICTED,
        pl.col("p_close_hat").is_not_null(),
        pl.col("game_date") >= first,
    )
    for r in forecast.iter_rows(named=True):
        found.setdefault(r["game_id"], []).append(
            (r["published_utc"].astimezone(UTC), float(r["p_close_hat"]))
        )
    return {k: sorted(v) for k, v in found.items()}


def pinnacle_sources(odds: pl.DataFrame) -> Sources:
    """Each game's Pinnacle h2h pairs (home side) by snapshot, both sides at most MAX_QUOTE_AGE
    old at it, de-vigged multiplicatively; a pair de-vigging refuses is none."""
    pairs = (
        odds.filter(
            pl.col("book") == BOOK, pl.col("market") == "h2h", pl.col("game_id").is_not_null()
        )
        .group_by("snapshot_utc", "game_id")
        .agg(
            home_price=pl.col("price_decimal").filter(pl.col("side") == "home").first(),
            away_price=pl.col("price_decimal").filter(pl.col("side") == "away").first(),
            oldest=pl.col("last_update_utc").min(),
        )
        .drop_nulls(["home_price", "away_price"])
        .filter(pl.col("snapshot_utc") - pl.col("oldest") <= MAX_QUOTE_AGE)
    )
    pairs = pairs.filter(1 / pl.col("home_price") + 1 / pl.col("away_price") >= 1)
    if pairs.is_empty():
        return {}
    fair = fair_probabilities(pairs.select("home_price", "away_price").to_numpy())[:, 0]
    found: Sources = {}
    for r, p in zip(pairs.iter_rows(named=True), fair, strict=True):
        found.setdefault(r["game_id"], []).append((r["snapshot_utc"].astimezone(UTC), float(p)))
    return {k: sorted(v) for k, v in found.items()}


def latest(source: Sequence[tuple[datetime, float]], at: datetime) -> tuple[datetime, float] | None:
    """The newest entry known strictly before at."""
    i = bisect_left(source, (at,)) - 1
    return source[i] if i >= 0 else None


Tops = dict[tuple[int, str], list[tuple[datetime, Decimal | None, Decimal | None]]]


def tops_of(tops: pl.DataFrame) -> Tops:
    """Each market's tops by observation (game_id, team): its best Yes bid and ask."""
    found: Tops = {}
    for r in tops.sort("observed_utc").iter_rows(named=True):
        bid = ks._price(r["yes_bid"]) if r["yes_bid"] is not None else None
        ask = ks._price(r["yes_ask"]) if r["yes_ask"] is not None else None
        found.setdefault((r["game_id"], r["team"]), []).append(
            (r["observed_utc"].astimezone(UTC), bid, ask)
        )
    return found


def top_at(
    series: Sequence[tuple[datetime, Decimal | None, Decimal | None]],
    at: datetime,
    max_age: timedelta = MAX_MID_AGE,
) -> tuple[datetime, Decimal | None, Decimal | None] | None:
    """The newest top observed at or before at, at most max_age old."""
    i = bisect_right(series, at, key=lambda top: top[0]) - 1
    if i < 0 or at - series[i][0] > max_age:
        return None
    return series[i]


def ages(game: "Game", at: datetime) -> tuple[timedelta, timedelta]:
    """How old a top and a book may be at at: the watch's cadence for the game's lead then."""
    if game.start_utc - at > kw.NEAR:
        return FAR_TOP_AGE, FAR_BOOK_AGE
    return MAX_MID_AGE, ks.MAX_BOOK_AGE


def book_at(market: ks.Market, at: datetime, max_age: timedelta) -> ks.Book | None:
    """The newest book observed at or before at, at most max_age old."""
    found = None
    for book in market.books:
        if book.observed_utc > at:
            break
        found = book
    if found is None or at - found.observed_utc > max_age:
        return None
    return found


def mid_of(top: tuple[datetime, Decimal | None, Decimal | None] | None) -> Decimal | None:
    if top is None or top[1] is None or top[2] is None:
        return None
    return (top[1] + top[2]) / 2


def change_times(games: Sequence[Game], dailyfaceoff: pl.DataFrame) -> dict[int, list[datetime]]:
    """Each game's moments its Daily Faceoff starter changed for either team: the polls at which
    the goalie named for the team differs, by name key, from the one named before. The goalie
    named at a poll is read from the polls observed by then alone: the latest naming one of the
    team's listing whose start is nearest the game's among them, as shadow._named reads it, so a
    later poll never changes an earlier change."""
    found: dict[int, list[datetime]] = {}
    if dailyfaceoff.is_empty():
        return found
    for game in games:
        for team in (game.home, game.away):
            polls = (
                dailyfaceoff.filter(pl.col("game_date") == game.game_date, pl.col("team") == team)
                .with_columns(
                    gap=(pl.col("start_utc") - pl.lit(game.start_utc)).abs(),
                    name=confirmed.key("goalie_name"),
                )
                .sort("observed_utc")
            )
            rows = list(polls.select("observed_utc", "gap", "name").iter_rows())
            before = None
            for t in sorted({r[0] for r in rows}):
                known = [r for r in rows if r[0] <= t]
                nearest = min(r[1] for r in known)
                named = [r[2] for r in known if r[1] == nearest and r[2] is not None]
                if not named:
                    continue
                if before is not None and named[-1] != before:
                    found.setdefault(game.game_id, []).append(t.astimezone(UTC))
                before = named[-1]
    return {k: sorted(v) for k, v in found.items()}


# The fills, kalshi_shadow.advance's rule over indexed logs


@dataclass(frozen=True)
class Logs:
    """One market's logs for the fills, by time: its trades (time, price, contracts) and its
    tops' asks (time, ask), each sorted, with their times apart for a search."""

    trades: tuple[tuple[datetime, Decimal, Decimal], ...]
    asks: tuple[tuple[datetime, Decimal], ...]
    trade_times: tuple[datetime, ...]
    ask_times: tuple[datetime, ...]

    @staticmethod
    def of(market: ks.Market) -> "Logs":
        return Logs(
            market.trades,
            market.asks,
            tuple(t for t, _, _ in market.trades),
            tuple(t for t, _ in market.asks),
        )


EMPTY_LOGS = Logs((), (), (), ())


def advance(
    bid: ks.Bid, until: datetime, logs: Logs, rules: Exchange
) -> list[tuple[datetime, Decimal, Decimal]]:
    """kalshi_shadow.advance over indexed logs: the bid's fills after its last advance up to
    until (and its expiry), each (time, contracts, cost at the maker fee). A trade at or below
    its price first takes the contracts ahead of it, then fills it; an ask at or below its price
    fills what is left."""
    end = min(until, bid.expires_utc)
    lo, hi = bisect_right(logs.trade_times, bid.since), bisect_right(logs.trade_times, end)
    events: list[tuple[datetime, int, Decimal]] = [
        (t, 0, count) for t, price, count in logs.trades[lo:hi] if price <= bid.price
    ]
    lo, hi = bisect_right(logs.ask_times, bid.since), bisect_right(logs.ask_times, end)
    events += [(t, 1, Decimal(0)) for t, ask in logs.asks[lo:hi] if ask <= bid.price]
    out = []
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


# One variant's day


@dataclass
class VariantDay:
    """One variant's state over a day: the resting bids, the contracts bought and the cost of the
    fills, each game's hold after a pull, and what happened."""

    fair: str
    h: Decimal
    # How long a MOVE pull holds the game (#385): WAIT for every variant of the record above.
    hold: str = WAIT
    bids: dict[tuple[int, str], ks.Bid] = field(default_factory=dict)
    bought: dict[tuple[int, str], Decimal] = field(default_factory=dict)
    filled_cost: Decimal = Decimal(0)
    # A pulled game's hold: when, and why.
    held: dict[int, tuple[datetime, str]] = field(default_factory=dict)
    # How many of each game's starter changes it has held the game for.
    starters: dict[int, int] = field(default_factory=dict)
    # Each resting bid's market's mid at its posting, which a MOVE pull reads.
    mids: dict[tuple[int, str], Decimal | None] = field(default_factory=dict)
    fills: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(
        default_factory=lambda: {"posted": 0, "pulled_starter": 0, "pulled_move": 0, "fills": 0}
    )
    largest_inventory: Decimal = Decimal(0)
    # A MOVE hold's market and its mid when the pulled bid was posted, which RETURN reads.
    moved: dict[int, tuple[tuple[int, str], Decimal]] = field(default_factory=dict)
    # Each market's last step with a bid resting, and the seconds bids rested: in all, and in
    # each game's LATE before its stop.
    seen: dict[tuple[int, str], datetime] = field(default_factory=dict)
    quoted_s: float = 0.0
    late_s: float = 0.0

    def resting_cost(self, rules: Exchange) -> Decimal:
        return sum(
            (ex.cost(b.left, b.price, rules, maker=True) for b in self.bids.values()), Decimal(0)
        )


@dataclass(frozen=True)
class Inputs:
    """What a day's variants read, indexed."""

    games: tuple[Game, ...]
    models: Sources
    pinnacle: Sources
    tops: Tops
    logs: Mapping[tuple[int, str], Logs]
    books: Mapping[tuple[int, str], ks.Market]
    changes: Mapping[int, list[datetime]]
    forecasts: Sources = field(default_factory=dict)
    forecasts2: Sources = field(default_factory=dict)

    def forecast(self, fair: str) -> Sources:
        """The close forecasts the fair value reads: version 1's or version 2's."""
        return self.forecasts2 if fair == FORECAST2 else self.forecasts


def fair_at(
    kind: str, game: Game, team: str, at: datetime, inputs: Inputs
) -> tuple[float, datetime, datetime | None] | None:
    """The team's fair value at at, with its model or Pinnacle source's time and, for anchored,
    its mid's: None without one."""
    if kind in (PINNACLE, *FORECASTS):
        source = inputs.pinnacle if kind == PINNACLE else inputs.forecast(kind)
        found = latest(source.get(game.game_id, []), at)
        return (game.side(team, found[1]), found[0], None) if found else None
    model = latest(inputs.models.get(game.game_id, []), at)
    if model is None:
        return None
    p = game.side(team, model[1])
    if kind == MODEL:
        return p, model[0], None
    top = top_at(inputs.tops.get((game.game_id, team), []), at, ages(game, at)[0])
    mid = mid_of(top)
    if mid is None or not Decimal(0) < mid < Decimal(1) or top is None:
        return None
    blended = sigmoid(ANCHOR_WEIGHT * logit(p) + (1 - ANCHOR_WEIGHT) * logit(float(mid)))
    return float(blended), model[0], top[0]


def _released(state: VariantDay, game: Game, at: datetime, why: str | None) -> None:
    """The game's bids cancelled, and with a reason it is held: a pull, if a bid was resting."""
    pulled = any((game.game_id, team) in state.bids for team in (game.home, game.away))
    for team in (game.home, game.away):
        state.bids.pop((game.game_id, team), None)
        state.mids.pop((game.game_id, team), None)
    if why is not None:
        state.held[game.game_id] = (at, why)
        state.moved.pop(game.game_id, None)
        if pulled:
            state.counts[f"pulled_{why}"] += 1


def _still_held(
    state: VariantDay,
    game: Game,
    fair: tuple[float, datetime, datetime | None],
    at: datetime,
    inputs: Inputs,
) -> bool:
    """Whether a pulled game still waits: for its fair value's source to update after the pull,
    or, after a MOVE, for the variant's hold rule to end it (#385)."""
    hold = state.held.get(game.game_id)
    if hold is None:
        return False
    pulled, why = hold
    source = fair[2] if state.fair == ANCHORED and why == MOVE else fair[1]
    if (source is not None and source > pulled) or (
        why == MOVE and _move_over(state, game, pulled, at, inputs)
    ):
        del state.held[game.game_id]
        state.moved.pop(game.game_id, None)
        return False
    return True


def _move_over(
    state: VariantDay, game: Game, pulled: datetime, at: datetime, inputs: Inputs
) -> bool:
    """Whether the variant's hold rule ends a MOVE hold at at: COOL_OFF after the pull, or the
    moved market's mid back within PULL_MOVE of its mid at the pulled bid's posting. WAIT never
    ends one this way, and TRUST never pulls on a move."""
    if state.hold == COOL:
        return at >= pulled + COOL_OFF
    if state.hold == RETURN and game.game_id in state.moved:
        key, then = state.moved[game.game_id]
        mid = mid_of(top_at(inputs.tops.get(key, []), at, ages(game, at)[0]))
        return mid is not None and abs(mid - then) < PULL_MOVE
    return False


def _rested(state: VariantDay, game: Game, key: tuple[int, str], at: datetime) -> None:
    """The time a market's bid rested since its last step, in all and in the game's LATE before
    its stop."""
    since = state.seen.get(key, at)
    state.seen[key] = at
    if at <= since:
        return
    state.quoted_s += (at - since).total_seconds()
    late_from = game.start_utc - STOP_BEFORE - LATE
    state.late_s += max(0.0, (at - max(since, late_from)).total_seconds())


def _quote_price(
    fair: float, h: Decimal, best_ask: Decimal | None, rounded: bool = True
) -> Decimal | None:
    """The bid: the fair value less h, floored to the cent, a cent below the best ask, none
    below a cent. Rounded, the fair value is first rounded to FAIR_PLACES decimals, so a float
    just below a cent (1 - 0.55 is 0.44999999999999996) floors to that cent (#376)."""
    value = Decimal(str(round(fair, FAIR_PLACES) if rounded else fair))
    price = (value - h).quantize(CENT, rounding=ROUND_FLOOR)
    if best_ask is not None:
        price = min(price, best_ask - CENT)
    return price if price >= CENT else None


def step_game(state: VariantDay, game: Game, at: datetime, inputs: Inputs, rules: Exchange) -> None:
    """One step of one game at at: its bids' fills to then, its pulls, and its quotes."""
    keys = [(game.game_id, team) for team in (game.home, game.away)]
    filled = []
    for key in keys:
        bid = state.bids.get(key)
        if bid is not None:
            logs = inputs.logs.get(key, EMPTY_LOGS)
            found = advance(bid, at, logs, rules)
            filled += [(t, key, bid, q, k) for t, q, k in found]
            # A bid filled in full rested until its last fill; this step replaces or drops it.
            _rested(state, game, key, found[-1][0] if found and bid.left <= 0 else at)
    # Both teams' fills in the order they came, so the inventory is measured after each.
    for t, key, bid, q, k in sorted(filled, key=lambda fill: (fill[0], fill[1][1])):
        state.bought[key] = state.bought.get(key, Decimal(0)) + q
        state.filled_cost += k
        state.counts["fills"] += 1
        state.fills.append(
            {
                "game_id": game.game_id,
                "team": key[1],
                "posted_utc": bid.placed_utc,
                "fill_utc": t,
                "contracts": q,
                "price": bid.price,
                "cost_usd": k,
                "fair_at_post": bid.p_side,
            }
        )
        net = state.bought[key] - state.bought.get((game.game_id, game.other(key[1])), Decimal(0))
        state.largest_inventory = max(state.largest_inventory, abs(net))
    if at >= game.start_utc - STOP_BEFORE:
        _released(state, game, at, None)
        return
    # A starter change holds the game whether or not a bid rests: its fair value came before it.
    arrived = bisect_right(inputs.changes.get(game.game_id, []), at)
    if arrived > state.starters.get(game.game_id, 0):
        state.starters[game.game_id] = arrived
        _released(state, game, at, STARTER)
    top_age, book_age = ages(game, at)
    for key in keys:
        bid, then = state.bids.get(key), state.mids.get(key)
        if bid is None or then is None or state.hold == TRUST:
            continue
        mid = mid_of(top_at(inputs.tops.get(key, []), at, top_age))
        if mid is not None and abs(mid - then) >= PULL_MOVE:
            _released(state, game, at, MOVE)
            state.moved[game.game_id] = (key, then)
            break
    for key in keys:
        team = key[1]
        fair = fair_at(state.fair, game, team, at, inputs)
        bid = state.bids.get(key)
        if fair is None or _still_held(state, game, fair, at, inputs):
            state.bids.pop(key, None)
            continue
        # A quote needs a recent top: post-only, it is never at or above the best ask.
        top = top_at(inputs.tops.get(key, []), at, top_age)
        if top is None:
            state.bids.pop(key, None)
            continue
        price = _quote_price(fair[0], state.h, top[2], game.game_date >= ROUNDED_FROM)
        bought = state.bought.get(key, Decimal(0))
        net = bought - state.bought.get((game.game_id, game.other(team)), Decimal(0))
        # A partial fill can leave less than SIZE to the cap: the bid is only what is left.
        size = min(SIZE, NET_CAP - net)
        if price is None or size <= 0:
            state.bids.pop(key, None)
            continue
        if bid is not None and bid.price == price and bid.left == size:
            continue
        state.bids.pop(key, None)
        book = book_at(inputs.books.get(key, ks.EMPTY), at, book_age)
        if book is None:
            continue
        planned = ex.cost(size, price, rules, maker=True)
        if state.filled_cost + state.resting_cost(rules) + planned > DAY_CAP:
            continue
        state.mids[key] = mid_of(top)
        state.seen[key] = at
        state.bids[key] = ks.Bid(
            game_id=game.game_id,
            team=team,
            p_side=fair[0],
            hurdle=0.0,
            price=price,
            left=size,
            posted_utc=at,
            queue=book.bids.get(price, Decimal(0)),
            expires_utc=game.start_utc - STOP_BEFORE,
            placed_utc=at,
        )
        state.counts["posted"] += 1


def steps(game: Game, inputs: Inputs, fair: str | None = None) -> list[datetime]:
    """A game's steps for the variant of fair: every top of either team's market and every update
    of its fair values, before STOP_BEFORE its start, and that moment itself. The close forecast's
    publications are steps of the forecast variant only, so the other fair values step as before
    it (ADR 0054)."""
    end = game.start_utc - STOP_BEFORE
    times = {
        t
        for team in (game.home, game.away)
        for t, _, _ in inputs.tops.get((game.game_id, team), [])
    }
    times |= {t for t, _ in inputs.models.get(game.game_id, [])}
    times |= {t for t, _ in inputs.pinnacle.get(game.game_id, [])}
    if fair in FORECASTS:
        times |= {t for t, _ in inputs.forecast(fair).get(game.game_id, [])}
    times |= set(inputs.changes.get(game.game_id, []))
    # A source published at t is known just after it (latest() reads strictly before).
    return sorted({t + timedelta(microseconds=1) for t in times if t < end} | {end})


def run_variant(
    fair: str, h: Decimal, inputs: Inputs, rules: Exchange, hold: str = WAIT
) -> VariantDay:
    """One variant over the day: every game's steps in time order, so the day's cap sees every
    game's bids and fills as they came."""
    state = VariantDay(fair, h, hold)
    events = sorted(
        ((t, g) for g in inputs.games for t in steps(g, inputs, fair)),
        key=lambda event: (event[0], event[1].game_id),
    )
    for at, game in events:
        step_game(state, game, at, inputs, rules)
    return state


# The day


@dataclass(frozen=True)
class DayResult:
    fills: pl.DataFrame
    counts: dict[str, Any]


def inputs_of(
    ledger: pl.DataFrame,
    morning: pl.DataFrame,
    odds: pl.DataFrame,
    depth: pl.DataFrame,
    trades: pl.DataFrame,
    tops: pl.DataFrame,
    dailyfaceoff: pl.DataFrame,
    forecasts: pl.DataFrame | None = None,
) -> Inputs:
    games = tuple(slate_of(ledger))
    books = ks.markets(depth, trades, tops)
    return Inputs(
        games,
        model_sources(ledger, morning),
        pinnacle_sources(odds),
        tops_of(tops),
        {key: Logs.of(market) for key, market in books.items()},
        books,
        change_times(games, dailyfaceoff),
        forecast_sources(forecasts) if forecasts is not None else {},
        forecast_sources(forecasts, FORECAST2) if forecasts is not None else {},
    )


def _mid_after(series: Sequence[Any], at: datetime, start: datetime) -> float | None:
    """The team's mid at at (the newest top at or before it), when that is before the start."""
    if at >= start:
        at = start - timedelta(microseconds=1)
    mid = mid_of(top_at(series, at))
    return float(mid) if mid is not None else None


def markouts(
    fills: list[dict[str, Any]],
    inputs: Inputs,
    references: pl.DataFrame,
    results: pl.DataFrame,
) -> list[dict[str, Any]]:
    """Each fill with its mids MARKOUTS after it and the last before the start, its game's
    Pinnacle closing proxy for its team and whether that was observed after the fill, and
    whether its team won on the full game."""
    games = {g.game_id: g for g in inputs.games}
    refs = {r["game_id"]: r for r in references.iter_rows(named=True)}
    won = {r["game_id"]: r["home_win"] for r in results.iter_rows(named=True)}
    out = []
    for fill in fills:
        game = games[fill["game_id"]]
        series = inputs.tops.get((game.game_id, fill["team"]), [])
        ref = refs.get(game.game_id)
        p_close = ref["p_close_home"] if ref else None
        home_win = won.get(game.game_id)
        out.append(
            fill
            | {
                "mid_5m": _mid_after(series, fill["fill_utc"] + MARKOUTS[0], game.start_utc),
                "mid_30m": _mid_after(series, fill["fill_utc"] + MARKOUTS[1], game.start_utc),
                "mid_last": _mid_after(series, game.start_utc, game.start_utc),
                "p_close": game.side(fill["team"], p_close) if p_close is not None else None,
                "close_after_fill": (
                    ref["close_snapshot_utc"] > fill["fill_utc"]
                    if ref and ref["close_snapshot_utc"] is not None
                    else None
                ),
                "won": (
                    None
                    if home_win is None
                    else int(home_win if fill["team"] == game.home else 1 - home_win)
                ),
            }
        )
    return out


def run_day(
    day: date,
    ledger: pl.DataFrame,
    morning: pl.DataFrame,
    odds: pl.DataFrame,
    depth: pl.DataFrame,
    trades: pl.DataFrame,
    tops: pl.DataFrame,
    dailyfaceoff: pl.DataFrame,
    games: pl.DataFrame,
    rules: Exchange,
    now: datetime,
    forecasts: pl.DataFrame | None = None,
    holds: bool = False,
) -> DayResult:
    """Every variant's day from the day's ledger, morning view, close forecasts, odds, Kalshi
    logs and Daily Faceoff polls; the fills valued with the results and the closing proxies known
    by now. With holds, the hold rules' variants instead (#385), each with its minutes quoted,
    and the day's slate (game_id, season) for their paired measure."""
    inputs = inputs_of(ledger, morning, odds, depth, trades, tops, dailyfaceoff, forecasts)
    references = lf.references(odds, now) if odds.height else pl.DataFrame()
    results = outcomes(games.filter(pl.col("game_id").is_in([g.game_id for g in inputs.games])))
    rows, counts = [], {}
    seasons = {g.game_id: (g.season, g.game_date, g.start_utc) for g in inputs.games}
    specs = (
        [(hold_name(h, hold), MODEL, h, hold) for h, hold in hold_variants()]
        if holds
        else [(variant_name(fair, h), fair, h, WAIT) for fair, h in variants()]
    )
    for name, fair, h, hold in specs:
        state = run_variant(fair, h, inputs, rules, hold)
        valued = markouts(state.fills, inputs, references, results) if state.fills else []
        for fill in valued:
            season, game_date, start = seasons[fill["game_id"]]
            rows.append(
                {
                    "variant": name,
                    "fair": fair,
                    "half_spread": float(h),
                    "game_date": game_date,
                    "season": season,
                    "start_utc": start,
                    **{k: (float(v) if isinstance(v, Decimal) else v) for k, v in fill.items()},
                }
            )
        counts[name] = state.counts | {"largest_inventory": float(state.largest_inventory)}
        if holds:
            counts[name] |= {
                "quoted_minutes": state.quoted_s / 60,
                "late_minutes": state.late_s / 60,
            }
    if holds:
        counts["slate"] = sorted([g.game_id, g.season] for g in inputs.games)
    counts["games"] = len(inputs.games)
    counts["watch_logs"] = tops.height > 0
    counts["model_days"] = sum(1 for g in inputs.games if g.game_id in inputs.models)
    fills = pl.DataFrame(rows, schema=FILL_SCHEMA) if rows else pl.DataFrame(schema=FILL_SCHEMA)
    return DayResult(fills.select(list(FILL_SCHEMA)), counts)


# The record


def day_keys(day: date, prefix: str = PREFIX) -> tuple[str, str]:
    """Where a day's counts and fills are kept, each written once: the record's under PREFIX, the
    hold rules' under HOLDS_PREFIX."""
    base = f"{prefix}/days/{day.isoformat()}"
    return f"{base}.json", f"{base}-fills.parquet"


def stored_days(store: lb.Store, prefix: str = PREFIX) -> list[date]:
    """The days whose counts are kept: the last file a day writes."""
    found = []
    for key in store.keys(f"{prefix}/days"):
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
    prefix: str = PREFIX,
) -> None:
    """A day's files, written once: its fills, then its counts, the last."""
    counts_key, fills_key = day_keys(day, prefix)
    lb.put_once(store, fills_key, lb.parquet(result.fills))
    meta = {
        "shadow": SHADOW,
        "version": VERSION if prefix == PREFIX else f"holds/{HOLDS_VERSION}",
        "day": day.isoformat(),
        "computed_utc": now.isoformat(),
        "code_version": code_version,
        "counts": result.counts,
    }
    lb.put_once(store, counts_key, json.dumps(meta, sort_keys=True).encode())


def kept(store: lb.Store, prefix: str = PREFIX) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    """Every kept day's fills and counts."""
    fills, metas = [], []
    for day in stored_days(store, prefix):
        counts_key, fills_key = day_keys(day, prefix)
        metas.append(json.loads(store.get(counts_key)))
        fills.append(pl.read_parquet(io.BytesIO(store.get(fills_key))))
    return (pl.concat(fills) if fills else pl.DataFrame(schema=FILL_SCHEMA)), metas


def due(today: date, done: Iterable[date], first: date = FIRST_DAY) -> list[date]:
    """The game dates from first (FIRST_DAY, or HOLDS_FIRST_DAY for the hold rules) before today
    (US Eastern) not kept yet: a day is read once its games are over, the night after."""
    kept_days = set(done)
    found, day = [], first
    while day < today:
        if day not in kept_days:
            found.append(day)
        day += timedelta(days=1)
    return found


# The measures


def per_contract(fills: pl.DataFrame) -> pl.DataFrame:
    """Each fill's figures per contract: the spread captured, the markouts and the profit at
    settlement, the maker fee in it."""
    fee = pl.col("cost_usd") / pl.col("contracts") - pl.col("price")
    return fills.with_columns(
        spread=pl.col("fair_at_post") - pl.col("price"),
        markout_5m=pl.col("mid_5m") - pl.col("price"),
        markout_30m=pl.col("mid_30m") - pl.col("price"),
        markout_last=pl.col("mid_last") - pl.col("price"),
        markout_close=pl.when(pl.col("close_after_fill")).then(pl.col("p_close") - pl.col("price")),
        profit=pl.col("won").cast(pl.Float64) - pl.col("price") - fee,
    )


def pairs(fills: pl.DataFrame) -> pl.DataFrame:
    """Per variant and game, the contracts bought on both teams (the smaller side's), whose
    payout is fixed at a dollar each."""
    keys = ["variant", "game_date", "season", "game_id"]
    by_team = fills.group_by(*keys, "team").agg(pl.col("contracts").sum())
    return by_team.group_by(keys).agg(
        teams=pl.len(), matched=pl.when(pl.len() == 2).then(pl.col("contracts").min())
    )


MEASURES = ("spread", "markout_5m", "markout_30m", "markout_last", "markout_close", "profit")


def settled(fills: pl.DataFrame, games: pl.DataFrame) -> pl.DataFrame:
    """The kept fills with their teams' full-game results as games holds them now: a day kept
    before its results were public gets them at a later report. The kept files never change."""
    results = outcomes(games).join(games.select("game_id", "home"), on="game_id")
    home_win = pl.col("home_win")
    return (
        fills.drop("won")
        .join(results.select("game_id", "home", "home_win"), on="game_id", how="left")
        .with_columns(
            won=pl.when(pl.col("team") == pl.col("home"))
            .then(home_win)
            .otherwise(1 - home_win)
            .cast(pl.Int8)
        )
        .select(list(FILL_SCHEMA))
    )


def per_contract_estimate(frame: pl.DataFrame, value: str) -> dict[str, Any]:
    """value per contract, each fill weighted by its contracts, with its weekly block bootstrap
    interval (hard rule 7), or only the counts over fewer than the live report's MIN_WEEKS. The
    same weeks are drawn for the contracts and their total, so each draw is a ratio of the two:
    splitting a fill in two changes nothing."""
    if not lr.enough(frame):
        return lr.counts(frame)
    weighted = frame.with_columns(total=pl.col(value) * pl.col("contracts"))
    totals = metrics.resampled_means(weighted, "total", np.random.default_rng(metrics.SEED))
    sizes = metrics.resampled_means(weighted, "contracts", np.random.default_rng(metrics.SEED))
    whole = float(weighted["total"].sum()) / float(weighted["contracts"].sum())
    spread = metrics.interval(whole, totals / sizes)
    return {"mean": spread.value, "low": spread.low, "high": spread.high} | lr.counts(frame)


def summary(
    fills: pl.DataFrame,
    metas: Sequence[Mapping[str, Any]],
    games: pl.DataFrame,
    holds: bool = False,
) -> dict[str, Any]:
    """The private figures over every kept day, per variant: its counts, and each measure per
    contract, with the results games holds now (settled). With holds, the hold rules' variants
    (#385), each with its minutes quoted, and the primary measure: each rule's profit per game
    less wait's (paired)."""
    rows = per_contract(settled(fills, games))
    matched = pairs(rows)
    result: dict[str, Any] = {
        "shadow": SHADOW,
        "version": f"holds/{HOLDS_VERSION}" if holds else VERSION,
        "days": len(metas),
        "days_with_watch_logs": sum(1 for m in metas if m["counts"].get("watch_logs")),
    }
    names = (
        [hold_name(h, hold) for h, hold in hold_variants()]
        if holds
        else [variant_name(fair, h) for fair, h in variants()]
    )
    for name in names:
        of_variant = rows.filter(pl.col("variant") == name)
        counts = [m["counts"].get(name, {}) for m in metas]
        figures: dict[str, Any] = {
            "posted": sum(c.get("posted", 0) for c in counts),
            "pulled_starter": sum(c.get("pulled_starter", 0) for c in counts),
            "pulled_move": sum(c.get("pulled_move", 0) for c in counts),
            "fills": of_variant.height,
            "contracts": float(of_variant["contracts"].sum()),
            "matched_contracts": float(
                matched.filter(pl.col("variant") == name)["matched"].fill_null(0).sum()
            ),
            "largest_inventory": max(
                (c.get("largest_inventory", 0.0) for c in counts), default=0.0
            ),
            "profit_usd": float((of_variant["profit"] * of_variant["contracts"]).sum()),
        }
        if holds:
            figures["quoted_minutes"] = sum(c.get("quoted_minutes", 0.0) for c in counts)
            figures["late_minutes"] = sum(c.get("late_minutes", 0.0) for c in counts)
        for measure in MEASURES:
            known = of_variant.filter(pl.col(measure).is_not_null())
            figures[measure] = per_contract_estimate(known, measure) if known.height else None
        result[name] = figures
    if holds:
        result["against_wait"] = against_wait(rows, metas, games)
    return result


def game_profits(
    rows: pl.DataFrame, metas: Sequence[Mapping[str, Any]], games: pl.DataFrame
) -> pl.DataFrame:
    """Each hold variant's profit at settlement per game (game_id, season, game_date, variant,
    profit_usd), the maker fee in it: every kept game whose result games holds now, zero where
    the variant had no fill."""
    known = outcomes(games).filter(pl.col("home_win").is_not_null())
    slate = pl.DataFrame(
        [
            {"game_id": g, "season": season, "game_date": date.fromisoformat(m["day"])}
            for m in metas
            for g, season in m["counts"].get("slate", [])
        ],
        schema={"game_id": pl.Int64, "season": pl.Int32, "game_date": pl.Date},
    ).join(known.select("game_id"), on="game_id")
    names = pl.DataFrame({"variant": [hold_name(h, hold) for h, hold in hold_variants()]})
    totals = (
        rows.filter(pl.col("profit").is_not_null())
        .group_by("variant", "game_id")
        .agg(profit_usd=(pl.col("profit") * pl.col("contracts")).sum())
    )
    return (
        slate.join(names, how="cross")
        .join(totals, on=["variant", "game_id"], how="left")
        .with_columns(pl.col("profit_usd").fill_null(0.0))
    )


def against_wait(
    rows: pl.DataFrame, metas: Sequence[Mapping[str, Any]], games: pl.DataFrame
) -> dict[str, Any]:
    """Each rule's profit per game less wait's on the same games, at each half-spread, with its
    weekly block bootstrap interval, or the counts only under the live report's MIN_WEEKS. At
    PRIMARY_HALF_SPREAD, the primary measure, Holm's adjustment over the three rules once each
    has its interval (#385)."""
    profits = game_profits(rows, metas, games)
    found: dict[str, Any] = {}
    p_values: dict[str, float] = {}
    for h in HALF_SPREADS:
        base = profits.filter(pl.col("variant") == hold_name(h, WAIT)).select(
            "game_id", "season", "game_date", wait=pl.col("profit_usd")
        )
        for hold in HOLDS[1:]:
            name = hold_name(h, hold)
            paired = (
                profits.filter(pl.col("variant") == name)
                .join(base, on=["game_id", "season", "game_date"])
                .with_columns(difference=pl.col("profit_usd") - pl.col("wait"))
            )
            if not lr.enough(paired):
                found[name] = lr.counts(paired)
                continue
            found[name] = metrics.bootstrap(paired, "difference").to_dict()
            if h == PRIMARY_HALF_SPREAD:
                p_values[name] = metrics.p_value(paired, "difference")
    if len(p_values) == len(HOLDS) - 1:
        found["holm"] = metrics.holm(p_values)
    return found


def count_line(metas: Sequence[Mapping[str, Any]], computed: int) -> str:
    """The log's line: counts only, never a price, a cost or a figure (#280)."""
    with_logs = sum(1 for m in metas if m["counts"].get("watch_logs"))
    fills = sum(
        m["counts"].get(variant_name(f, h), {}).get("fills", 0)
        for m in metas
        for f, h in variants()
    )
    return (
        f"kalshi quotes: {computed} days computed now, {len(metas)} kept ({with_logs} with watch "
        f"logs), {fills} fills over {len(variants())} variants"
    )


def holds_count_line(metas: Sequence[Mapping[str, Any]], computed: int) -> str:
    """The hold rules' log line: counts only (#280)."""
    fills = sum(
        m["counts"].get(hold_name(h, hold), {}).get("fills", 0)
        for m in metas
        for h, hold in hold_variants()
    )
    return (
        f"kalshi quote holds: {computed} days computed now, {len(metas)} kept, {fills} fills "
        f"over {len(hold_variants())} variants"
    )


def report_key(as_of: datetime, prefix: str = PREFIX) -> str:
    return f"{prefix}/reports/{as_of:%Y-%m-%dT%H%M%SZ}.json"


# The dashboard (#398)

# The records' names on the dashboard: the simulator's and the hold rules'.
RECORD, HOLDS_RECORD = f"{SHADOW}/{VERSION}", f"{SHADOW}/holds/{HOLDS_VERSION}"
DAY_KEY = ("game_date", "record", "variant")
REPORT_KEY = ("as_of", "record")
DAY_SCHEMA = {
    "game_date": pl.Date,
    "record": pl.String,
    "variant": pl.String,
    "fair": pl.String,
    "half_spread_cents": pl.Int16,
    "hold": pl.String,
    "games": pl.Int64,
    "watch_logs": pl.Boolean,
    "posted": pl.Int64,
    "pulled_move": pl.Int64,
    "pulled_starter": pl.Int64,
    "fills": pl.Int64,
    "contracts": pl.Float64,
    "matched_contracts": pl.Float64,
    "largest_inventory": pl.Float64,
    "cost_usd": pl.Float64,
    "fees_usd": pl.Float64,
    "edge_fair_usd": pl.Float64,
    "last_contracts": pl.Float64,
    "edge_last_usd": pl.Float64,
    "close_contracts": pl.Float64,
    "edge_close_usd": pl.Float64,
    "settled_contracts": pl.Float64,
    "payout_usd": pl.Float64,
    "profit_usd": pl.Float64,
    "quoted_minutes": pl.Float64,
    "late_minutes": pl.Float64,
    "kept_utc": UTC_TYPE,
    "code_version": pl.String,
}
# The sums day_figures adds per game date and variant, zero on a day without a fill.
SUMS = (
    "contracts",
    "matched_contracts",
    "cost_usd",
    "fees_usd",
    "edge_fair_usd",
    "last_contracts",
    "edge_last_usd",
    "close_contracts",
    "edge_close_usd",
    "settled_contracts",
    "payout_usd",
)


def scored_from(fair: str) -> date:
    """The first game date a fair value quotes on: the close forecasts' from the day after their
    registration, the others' from FIRST_DAY."""
    return {FORECAST: FORECAST_FIRST_DAY, FORECAST2: FORECAST2_FIRST_DAY}.get(fair, FIRST_DAY)


def _net(value: str) -> pl.Expr:
    """A measure in USD over a fill's contracts, net of its maker fee: the profit it expects."""
    return pl.col(value) * pl.col("contracts") - pl.col("fee")


def day_figures(
    fills: pl.DataFrame,
    metas: Sequence[Mapping[str, Any]],
    games: pl.DataFrame,
    holds: bool = False,
) -> pl.DataFrame:
    """Per kept game date and variant, the dashboard's figures (#398), with the results games
    holds now (settled): the day's counts, the contracts and their cost with the maker fee, and
    the profit expected at posting by the variant's fair value (spread), by Kalshi's last mid
    before the start (markout_last) and by Pinnacle's close after the fill (markout_close), each
    net of the fee on the contracts it is known for; then the payout and the profit at
    settlement, null until every fill's game has its result. Every variant has a row on each kept
    day from its fair value's first, with or without a fill. Sums only: no price, ticker, time or
    trade of a fill (#280)."""
    rows = per_contract(settled(fills, games)).with_columns(
        fee=pl.col("cost_usd") - pl.col("price") * pl.col("contracts")
    )
    last, close, won = (pl.col(c).is_not_null() for c in ("markout_last", "markout_close", "won"))
    sums = rows.group_by("game_date", "variant").agg(
        fills=pl.len(),
        contracts=pl.col("contracts").sum(),
        cost_usd=pl.col("cost_usd").sum(),
        fees_usd=pl.col("fee").sum(),
        edge_fair_usd=_net("spread").sum(),
        last_contracts=pl.col("contracts").filter(last).sum(),
        edge_last_usd=_net("markout_last").filter(last).sum(),
        close_contracts=pl.col("contracts").filter(close).sum(),
        edge_close_usd=_net("markout_close").filter(close).sum(),
        settled_contracts=pl.col("contracts").filter(won).sum(),
        payout_usd=(pl.col("won") * pl.col("contracts")).filter(won).sum(),
        unsettled=won.not_().sum(),
    )
    matched = (
        pairs(rows)
        .group_by("game_date", "variant")
        .agg(matched_contracts=pl.col("matched").fill_null(0).sum())
    )
    specs = (
        [(hold_name(h, hold), MODEL, h, hold) for h, hold in hold_variants()]
        if holds
        else [(variant_name(fair, h), fair, h, None) for fair, h in variants()]
    )
    skeleton = []
    for meta in metas:
        day = date.fromisoformat(meta["day"])
        for name, fair, h, hold in specs:
            if day < scored_from(fair):
                continue
            counts = meta["counts"].get(name, {})
            skeleton.append(
                {
                    "game_date": day,
                    "record": HOLDS_RECORD if holds else RECORD,
                    "variant": name,
                    "fair": fair,
                    "half_spread_cents": int(h * 100),
                    "hold": hold,
                    "games": meta["counts"].get("games", 0),
                    "watch_logs": bool(meta["counts"].get("watch_logs")),
                    "posted": counts.get("posted", 0),
                    "pulled_move": counts.get("pulled_move", 0),
                    "pulled_starter": counts.get("pulled_starter", 0),
                    "largest_inventory": float(counts.get("largest_inventory", 0.0)),
                    "quoted_minutes": counts.get("quoted_minutes") if holds else None,
                    "late_minutes": counts.get("late_minutes") if holds else None,
                    "kept_utc": datetime.fromisoformat(meta["computed_utc"]),
                    "code_version": meta["code_version"],
                }
            )
    base_schema = {k: v for k, v in DAY_SCHEMA.items() if k not in (*SUMS, "fills", "profit_usd")}
    frame = pl.DataFrame(skeleton, schema=base_schema)
    if not frame.height:
        return pl.DataFrame(schema=DAY_SCHEMA)
    joined = (
        frame.join(sums, on=["game_date", "variant"], how="left")
        .join(matched, on=["game_date", "variant"], how="left")
        .with_columns(pl.col(*SUMS).fill_null(0.0), pl.col("fills", "unsettled").fill_null(0))
    )
    return joined.with_columns(
        profit_usd=pl.when(pl.col("unsettled") == 0).then(pl.col("payout_usd") - pl.col("cost_usd"))
    ).select([pl.col(c).cast(t) for c, t in DAY_SCHEMA.items()])


def _finite(value: Any) -> Any:
    """value with every float that isn't finite as null: JSON has no NaN."""
    if isinstance(value, float):
        return value if np.isfinite(value) else None
    if isinstance(value, Mapping):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_finite(v) for v in value]
    return value


def report_record(figures: Mapping[str, Any], as_of: date, holds: bool = False) -> dict[str, Any]:
    """A night's summary as a quote_reports row for the dashboard (#398)."""
    return {
        "as_of": as_of.isoformat(),
        "record": HOLDS_RECORD if holds else RECORD,
        "figures": _finite(json.loads(json.dumps(figures, default=str))),
        "code_version": figures["code_version"],
    }
