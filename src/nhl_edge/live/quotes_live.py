"""The Market making tab's Today view (#398 step 3): the quoting simulator on today's games as
they stand, every few minutes while the watch runs, for the owner-only dashboard.

The simulator's own rules (live/kalshi_quotes.py), unchanged: each variant runs today's steps up
to now (run_variant's until) on the watch's polls so far, read from the watch's own raw store on
its runner, never from Kalshi. Each game and team then gets one row (LIVE_SCHEMA): our fair value,
Kalshi's latest top, our resting bid, what we hold and at what cost, and the game's state. The
record stays the nightly one: these rows are a view of the day, replaced at each cycle, and
nothing reads them back.

**Private.** The rows carry Kalshi's prices, so they go only to the owner-only quote_live table:
the owner chose on 2026-11-05 to see the live view with Kalshi's prices on the dashboard. The log
prints counts only (#280).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import polars as pl

from nhl_edge.ingest import kalshi_watch as kw
from nhl_edge.lake.raw import RawStore
from nhl_edge.live import kalshi_quotes as kq
from nhl_edge.live.real import Exchange

# The live variants: our model's fair value at each half-spread, the demo's (ADR 0055).
LIVE = tuple((kq.MODEL, h) for h in kq.HALF_SPREADS)
EVERY = timedelta(minutes=5)
# The lake's tables a cycle reads beside the watch's polls: the fair values' sources. The
# starters come from Daily Faceoff's pages of the day, as a live decision reads them (ADR 0037):
# the lake's table is rebuilt only at night.
LAKE_TABLES = (
    "paper_ledger",
    "morning_predictions",
    "close_forecasts",
    "odds_snapshots",
)
TABLE = "quote_live"
KEY = ("game_date", "variant", "game_id", "team")
# A game's state at the cycle, the dashboard's chip.
WAITING, QUOTING, PULLED_MOVE, PULLED_STARTER, IDLE, STOPPED, STARTED = (
    "waiting",
    "quoting",
    "pulled_move",
    "pulled_starter",
    "not_quoting",
    "stopped",
    "started",
)
STATES = (WAITING, QUOTING, PULLED_MOVE, PULLED_STARTER, IDLE, STOPPED, STARTED)
UTC_TYPE = pl.Datetime("us", "UTC")
LIVE_SCHEMA = {
    "game_date": pl.Date,
    "variant": pl.String,
    "half_spread_cents": pl.Int16,
    "game_id": pl.Int64,
    "team": pl.String,
    "home": pl.Boolean,
    "opponent": pl.String,
    "start_utc": UTC_TYPE,
    "state": pl.String,
    "fair": pl.Float64,
    "yes_bid": pl.Float64,
    "yes_ask": pl.Float64,
    "top_utc": UTC_TYPE,
    "bid_price": pl.Float64,
    "bid_left": pl.Float64,
    "contracts": pl.Float64,
    "cost_usd": pl.Float64,
    "fills": pl.Int64,
    "updated_utc": UTC_TYPE,
    "code_version": pl.String,
}


@dataclass
class Watched:
    """A game date's watch polls, parsed once each as they arrive: a poll still being written
    (no meta yet) is read at a later cycle."""

    store: RawStore
    day: date
    read: set[str] = field(default_factory=set)
    frames: dict[str, list[pl.DataFrame]] = field(
        default_factory=lambda: {kind: [] for kind in kw.KINDS}
    )
    unparsed: int = 0

    def update(self) -> dict[str, pl.DataFrame]:
        """The day's tables with every complete poll so far."""
        report = kw.ReplayReport()
        for kind in kw.KINDS:
            for raw_key in kw.poll_keys(self.store, kind, self.day):
                if raw_key in self.read:
                    continue
                found = kw.read_poll(self.store, kind, raw_key, report)
                if found is None:
                    continue
                self.read.add(raw_key)
                self.frames[kind] += found
        self.unparsed += len(report.unparsed)
        return kw.watch_tables(self.frames)


def state_of(
    state: kq.VariantDay, game: kq.Game, team: str, fair: float | None, now: datetime
) -> str:
    if now >= game.start_utc:
        return STARTED
    if now >= game.start_utc - kq.STOP_BEFORE:
        return STOPPED
    held = state.held.get(game.game_id)
    if held is not None:
        return PULLED_MOVE if held[1] == kq.MOVE else PULLED_STARTER
    if (game.game_id, team) in state.bids:
        return QUOTING
    return WAITING if fair is None else IDLE


def snapshot(
    state: kq.VariantDay,
    inputs: kq.Inputs,
    now: datetime,
    code_version: str,
) -> list[dict[str, Any]]:
    """One row per game and team of the variant's day as it stands at now."""
    costs: dict[tuple[int, str], Decimal] = {}
    fills: dict[tuple[int, str], int] = {}
    for f in state.fills:
        key = (f["game_id"], f["team"])
        costs[key] = costs.get(key, Decimal(0)) + Decimal(f["cost_usd"])
        fills[key] = fills.get(key, 0) + 1
    rows = []
    # A source published at t is known just after it, as the steps read it.
    at = now + timedelta(microseconds=1)
    for game in inputs.games:
        for team in (game.home, game.away):
            key = (game.game_id, team)
            found = kq.fair_at(state.fair, game, team, at, inputs)
            fair = found[0] if found else None
            series = inputs.tops.get(key, [])
            top = kq.top_at(series, now, timedelta.max) if series else None
            bid = state.bids.get(key)
            rows.append(
                {
                    "game_date": game.game_date,
                    "variant": kq.variant_name(state.fair, state.h),
                    "half_spread_cents": int(state.h * 100),
                    "game_id": game.game_id,
                    "team": team,
                    "home": team == game.home,
                    "opponent": game.other(team),
                    "start_utc": game.start_utc,
                    "state": state_of(state, game, team, fair, now),
                    "fair": fair,
                    "yes_bid": float(top[1]) if top and top[1] is not None else None,
                    "yes_ask": float(top[2]) if top and top[2] is not None else None,
                    "top_utc": top[0] if top else None,
                    "bid_price": float(bid.price) if bid else None,
                    "bid_left": float(bid.left) if bid else None,
                    "contracts": float(state.bought.get(key, Decimal(0))),
                    "cost_usd": float(costs.get(key, Decimal(0))),
                    "fills": fills.get(key, 0),
                    "updated_utc": now,
                    "code_version": code_version,
                }
            )
    return rows


def known_by(frame: pl.DataFrame, now: datetime) -> pl.DataFrame:
    """The rows observed by now: a trade is indexed by when it was made, so one fetched later
    would otherwise fill a bid before it was known (hard rule 1)."""
    return frame.filter(pl.col("observed_utc") <= now) if "observed_utc" in frame.columns else frame


def live_inputs(
    day: date,
    read: Mapping[str, pl.DataFrame],
    watched: Mapping[str, pl.DataFrame],
    dailyfaceoff: pl.DataFrame,
    now: datetime,
) -> kq.Inputs:
    """Today's inputs as known at now, from the lake's tables (read), the watch's polls so far
    (watched) and Daily Faceoff's pages of the day: the decision's slate once it is published,
    the morning view's before."""
    dated = {
        table: read[table].filter(pl.col("game_date") == day)
        for table in ("paper_ledger", "morning_predictions", "close_forecasts")
    }
    ledger, morning = dated["paper_ledger"], dated["morning_predictions"]
    slate = ledger if ledger.height else morning
    ids = slate["game_id"].unique().implode()
    return kq.inputs_of(
        ledger,
        morning,
        read["odds_snapshots"].filter(pl.col("game_id").is_in(ids)),
        known_by(watched[kw.BOOKS].filter(pl.col("game_date") == day), now),
        known_by(watched[kw.TRADES].filter(pl.col("game_date") == day), now),
        known_by(watched[kw.TOPS].filter(pl.col("game_date") == day), now),
        known_by(dailyfaceoff.filter(pl.col("game_date") == day), now),
        dated["close_forecasts"],
        slate=slate,
    )


def live_rows(
    inputs: kq.Inputs,
    rules: Exchange,
    now: datetime,
    code_version: str,
    variants: Sequence[tuple[str, Decimal]] = LIVE,
) -> pl.DataFrame:
    """Every live variant's rows at now."""
    rows = []
    for fair, h in variants:
        state = kq.run_variant(fair, h, inputs, rules, until=now)
        rows += snapshot(state, inputs, now, code_version)
    return pl.DataFrame(rows, schema=LIVE_SCHEMA) if rows else pl.DataFrame(schema=LIVE_SCHEMA)


def count_line(rows: pl.DataFrame, now: datetime, unparsed: int) -> str:
    """The log's line: counts only, never a price or a quantity (#280)."""
    states = rows.group_by("state").len().sort("state").iter_rows() if rows.height else []
    by_state = ", ".join(f"{n} {s}" for s, n in states) or "no rows"
    games = rows["game_id"].n_unique() if rows.height else 0
    return (
        f"quotes live {now.astimezone(UTC):%H:%M}Z: {games} games, {rows.height} rows "
        f"({by_state}); {unparsed} unparsed polls"
    )
