"""The real Kalshi fills valued against the close (#318, ADR 0040): CLV only against a reference
observed after the fill.

A resting order fills until 5 minutes before the start (ADR 0040), while Pinnacle's closing proxy
can be taken up to 90 minutes before it (market/closing.MAX_LEAD). A fill observed at or after its
game's proxy has no later price to beat: valued against that earlier quote, its CLV would compare
the fill with an older price rather than with the close, and the fills after news land there most.
So, as the owner confirmed on 2026-10-23:
- an order's fills before the start (placement.final_record) are split by Kalshi's is_taker into
  taker and maker, and each part is one bid: its contracts and its cost, fees included;
- a part's fills observed before its game's proxy form the bid valued, CLV = p_close · contracts /
  cost - 1, p_close its team's side of Pinnacle's proxy pair de-vigged (market/devig.py, hard
  rule 3), as the paper bets' CLV (live/settle.py);
- a fill at or after the proxy (AFTER_REFERENCE), and every fill of a started game without one
  (NO_REFERENCE), gets no CLV and is counted apart, never valued against an earlier quote. A fill
  of a game not yet started waits (PENDING);
- the figures come for taker and maker apart, and by the delay from the order's placement to the
  bid's first fill (DELAY_EDGES), each with its weekly block bootstrap interval, counts only under
  4 weeks (hard rule 7).

They read Kalshi's fills, so they stay private (#280): `nhl real fills` writes them to R2 and
prints counts only.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import polars as pl

from nhl_edge.live import bundle as lb
from nhl_edge.live import placement, settle
from nhl_edge.live import report as lr
from nhl_edge.market import closing
from nhl_edge.market.devig import fair_probabilities

TAKER, MAKER, UNKNOWN = "taker", "maker", "unknown"
KINDS = (TAKER, MAKER, UNKNOWN)
VALUED = "valued"
AFTER_REFERENCE = "after the reference"
NO_REFERENCE = "no reference"
PENDING = "pending"
REFERENCES = (VALUED, AFTER_REFERENCE, NO_REFERENCE, PENDING)
# The delay bands' edges, from the order's placement to the bid's first fill, fixed before any
# real fill (#318): under a minute (a taker fill at placement), to an hour, to four hours, and
# beyond.
DELAY_EDGES = (timedelta(minutes=1), timedelta(hours=1), timedelta(hours=4))
DELAY_BANDS = ("under 1 minute", "1 minute to 1 hour", "1 to 4 hours", "4 hours or more")

FILL_SCHEMA = {
    "game_date": pl.Date,
    "season": pl.Int32,
    "game_id": pl.Int64,
    "team": pl.String,
    "start_utc": pl.Datetime("us", UTC),
    "placed_utc": pl.Datetime("us", UTC),
    "fill_utc": pl.Datetime("us", UTC),
    "kind": pl.String,
    "contracts": pl.Float64,
    "cost_usd": pl.Float64,
}


def _kind(fill: Mapping[str, Any]) -> str:
    taker = fill.get("is_taker")
    return UNKNOWN if taker is None else TAKER if taker else MAKER


def _sent(record: Mapping[str, Any]) -> str:
    """When the order went to Kalshi, for its fills' delay: Kalshi's own creation time when its
    order shows one (an ADR 0045 order's taker part's, sent first); else the placed record's time,
    unless the order was adopted, whose placed time is when a later run found it (Codex on #324),
    and then the order record's, written just before the send."""
    created = record["final"].get("order", {}).get("created_time")
    if created:
        return str(created).replace("Z", "+00:00")
    placed = record.get(placement.PLACED) or record.get(placement.TAKER_PLACED)
    if placed and not placed.get("adopted"):
        return placed["placed_utc"]
    return record["order"]["written_utc"]


def fills(store: lb.Store, env: str) -> pl.DataFrame:
    """One row per fill before the start of each order of an environment with its final record:
    its game, team, placement and fill times, kind, contracts and cost in USD with its fee. A fill
    at or after the start is never the bet's (hard rule 2, placement.final_record)."""
    rows = []
    for record in placement.stored_orders(store, env).values():
        order, final = record.get("order"), record.get("final")
        if order is None or final is None or "fills" not in final:
            continue
        start = datetime.fromisoformat(final["start_utc"]).astimezone(UTC)
        sent = _sent(record)
        for fill in final["fills"]:
            at = datetime.fromisoformat(fill["created_time"].replace("Z", "+00:00"))
            if at >= start:
                continue
            count = Decimal(str(fill.get("count_fp") or 0))
            price = Decimal(str(fill.get("yes_price_dollars") or 0))
            fee = Decimal(str(fill.get("fee_cost") or 0))
            rows.append(
                {
                    "game_date": datetime.fromisoformat(order["game_date"]).date(),
                    "season": order["season"],
                    "game_id": order["game_id"],
                    "team": order["team"],
                    "start_utc": start,
                    "placed_utc": datetime.fromisoformat(sent).astimezone(UTC),
                    "fill_utc": at.astimezone(UTC),
                    "kind": _kind(fill),
                    "contracts": float(count),
                    "cost_usd": float(count * price + fee),
                }
            )
    return pl.DataFrame(rows, schema=FILL_SCHEMA).filter(pl.col("contracts") > 0)


def references(odds: pl.DataFrame, now: datetime) -> pl.DataFrame:
    """Each started game's reference (game_id, close_status, close_snapshot_utc, p_close_home):
    Pinnacle's closing proxy (live/settle.closes), its pair de-vigged multiplicatively. A game the
    Odds API listed under two events keeps a proxy over none, and the later one."""
    events = closing.pinnacle_closes(odds, now).select("event_id", "game_id")
    found = (
        settle.closes(odds, now)
        .join(events, on="event_id")
        .drop_nulls("game_id")
        .sort(
            pl.col("close_status") == closing.PROXY,
            pl.col("close_snapshot_utc"),
            descending=True,
            nulls_last=True,
        )
        .unique(subset="game_id", keep="first", maintain_order=True)
    )
    known = found["close_home"].is_not_null() & found["close_away"].is_not_null()
    fair = np.full(found.height, np.nan)
    if known.any():
        pair = found.filter(known).select("close_home", "close_away").to_numpy()
        fair[known.to_numpy()] = fair_probabilities(pair)[:, 0]
    return found.with_columns(
        p_close_home=pl.Series(fair, dtype=pl.Float64).fill_nan(None),
    ).select("game_id", "close_status", "close_snapshot_utc", "p_close_home")


def referenced(
    found: pl.DataFrame, refs: pl.DataFrame, teams: pl.DataFrame, now: datetime
) -> pl.DataFrame:
    """Each fill with its reference status and its team's fair closing probability: VALUED when
    its game's proxy was observed after it, AFTER_REFERENCE when at or before it, NO_REFERENCE for
    a started game without a proxy (or whose teams aren't known), PENDING before the start. teams
    gives each game's home and away (game_id, home, away)."""
    joined = (
        found.join(teams.select("game_id", "home", "away"), on="game_id", how="left")
        .join(refs, on="game_id", how="left")
        .with_columns(
            p_close=pl.when(pl.col("team") == pl.col("home"))
            .then(pl.col("p_close_home"))
            .when(pl.col("team") == pl.col("away"))
            .then(1 - pl.col("p_close_home"))
        )
    )
    proxy = (pl.col("close_status") == closing.PROXY) & pl.col("p_close").is_not_null()
    return joined.with_columns(
        reference=pl.when(pl.col("start_utc") > pl.lit(now, dtype=FILL_SCHEMA["start_utc"]))
        .then(pl.lit(PENDING))
        .when(~proxy.fill_null(False))
        .then(pl.lit(NO_REFERENCE))
        .when(pl.col("close_snapshot_utc") > pl.col("fill_utc"))
        .then(pl.lit(VALUED))
        .otherwise(pl.lit(AFTER_REFERENCE)),
    )


def bids(rows: pl.DataFrame) -> pl.DataFrame:
    """The valued bids: each order's taker part and maker part, from its fills observed before
    its game's proxy, with its contracts, cost, first fill, delay band and CLV at its matched
    price (contracts per dollar, fees included)."""
    keys = ["game_date", "season", "game_id", "team", "kind"]
    valued = rows.filter(pl.col("reference") == VALUED)
    delay = pl.col("first_fill_utc") - pl.col("placed_utc")
    band = pl.lit(DELAY_BANDS[-1])
    for edge, name in reversed(list(zip(DELAY_EDGES, DELAY_BANDS[:-1], strict=True))):
        band = pl.when(delay < pl.lit(edge)).then(pl.lit(name)).otherwise(band)
    return (
        valued.group_by(keys, maintain_order=True)
        .agg(
            pl.col("contracts").sum(),
            pl.col("cost_usd").sum(),
            pl.col("placed_utc").first(),
            first_fill_utc=pl.col("fill_utc").min(),
            p_close=pl.col("p_close").first(),
        )
        .with_columns(band=band)
        .with_columns(clv=pl.col("p_close") * pl.col("contracts") / pl.col("cost_usd") - 1)
        .sort("game_date", "game_id", "kind")
    )


def summary(rows: pl.DataFrame) -> dict[str, Any]:
    """The private figures: for each kind, its fills by reference status, its valued bids and
    their CLV per bid, overall and by delay band, each through the live report's estimate (its
    weekly block bootstrap interval, counts only under 4 weeks)."""
    valued = bids(rows)
    result: dict[str, Any] = {
        "orders_filled": rows.select("game_date", "game_id").unique().height,
        "fills": rows.height,
    }
    for kind in KINDS:
        of_kind = rows.filter(pl.col("kind") == kind)
        if kind == UNKNOWN and of_kind.is_empty():
            continue
        kept = valued.filter(pl.col("kind") == kind)
        result[kind] = {
            "fills": {
                status: of_kind.filter(pl.col("reference") == status).height
                for status in REFERENCES
            },
            "bids": kept.height,
            "clv_per_bid": lr.estimate(kept, "clv") if kept.height else None,
            "by_delay": {
                name: {
                    "bids": (banded := kept.filter(pl.col("band") == name)).height,
                    "clv_per_bid": lr.estimate(banded, "clv") if banded.height else None,
                }
                for name in DELAY_BANDS
            },
        }
    return result


def count_line(env: str, rows: pl.DataFrame) -> str:
    """The log's line: counts only, never a price, a cost or a CLV (#280)."""
    parts = []
    for kind in KINDS:
        of_kind = rows.filter(pl.col("kind") == kind)
        if kind == UNKNOWN and of_kind.is_empty():
            continue
        by = {s: of_kind.filter(pl.col("reference") == s).height for s in REFERENCES}
        parts.append(
            f"{kind} {of_kind.height} ({by[VALUED]} {VALUED}, {by[AFTER_REFERENCE]} "
            f"{AFTER_REFERENCE}, {by[NO_REFERENCE]} {NO_REFERENCE}, {by[PENDING]} {PENDING})"
        )
    orders = rows.select("game_date", "game_id").unique().height
    return f"real fills {env}: {orders} orders filled; " + "; ".join(parts)


def report_key(env: str, as_of: datetime) -> str:
    """Where a run's private figures go: beside the environment's orders."""
    return f"{placement.PREFIXES[env]}/fill-reports/{as_of:%Y-%m-%dT%H%M%SZ}.json"
