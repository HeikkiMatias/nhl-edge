"""A SYNTHETIC day for shadow kalshi_prices (#320, ADR 0046): one game, its decision, Pinnacle's
pair at two slots, and Kalshi's books, trades and tops of both teams' markets. Nothing here comes
from Kalshi, the Odds API or Daily Faceoff."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from nhl_edge.betting.selection import POLICY_V2
from nhl_edge.live.real import Exchange

DAY = date(2026, 10, 27)
SEASON = 20262027
GAME = 2026020100
HOME, AWAY = "WSH", "PIT"
START = datetime(2026, 10, 27, 23, 0, tzinfo=UTC)  # 19:00 ET
MIDDAY = datetime(2026, 10, 27, 16, 45, 30, tzinfo=UTC)
PUBLISHED = MIDDAY + timedelta(minutes=8)
PRE7 = datetime(2026, 10, 27, 22, 45, 30, tzinfo=UTC)
RULES = Exchange("live", usd_per_eur=1.16)
TICKERS = {
    HOME: f"KXNHLGAME-26OCT27{AWAY}{HOME}-{HOME}",
    AWAY: f"KXNHLGAME-26OCT27{AWAY}{HOME}-{AWAY}",
}
UTC_US = pl.Datetime("us", UTC)


def ledger(**overrides: Any) -> pl.DataFrame:
    """The day's decision on the game: predicted, with B3 and u."""
    row = {
        "game_date": DAY,
        "season": SEASON,
        "game_id": GAME,
        "start_utc": START,
        "home": HOME,
        "away": AWAY,
        "prediction_utc": MIDDAY + timedelta(minutes=5),
        "published_utc": PUBLISHED,
        "status": "predicted",
        "policy_version": POLICY_V2,
        "decision_snapshot_utc": MIDDAY,
        "p_b0": 0.54,
        "p_b3": 0.58,
        "u_sd": 0.5,
    } | overrides
    return pl.DataFrame(
        [row],
        schema={
            "game_date": pl.Date,
            "season": pl.Int32,
            "game_id": pl.Int64,
            "start_utc": UTC_US,
            "home": pl.String,
            "away": pl.String,
            "prediction_utc": UTC_US,
            "published_utc": UTC_US,
            "status": pl.String,
            "policy_version": pl.String,
            "decision_snapshot_utc": UTC_US,
            "p_b0": pl.Float64,
            "p_b3": pl.Float64,
            "u_sd": pl.Float64,
        },
    )


def odds(
    pairs: Sequence[tuple[str, datetime, float, float]], age: timedelta = timedelta(seconds=30)
) -> pl.DataFrame:
    """Pinnacle's h2h pair at each (slot, snapshot, home price, away price), last updated age
    before its snapshot."""
    rows = []
    for slot, snapshot, home_price, away_price in pairs:
        for side, price in (("home", home_price), ("away", away_price)):
            rows.append(
                {
                    "snapshot_utc": snapshot,
                    "last_update_utc": snapshot - age,
                    "event_id": "e1",
                    "commence_time_utc": START,
                    "home": HOME,
                    "away": AWAY,
                    "book": "pinnacle",
                    "market": "h2h",
                    "side": side,
                    "line": None,
                    "price_decimal": price,
                    "is_closing_proxy": False,
                    "slot": slot,
                    "raw_key": f"odds/{snapshot:%Y%m%dT%H%M%SZ}",
                    "snapshot_date": snapshot.date(),
                    "game_id": GAME,
                    "game_type": 2,
                }
            )
    return pl.DataFrame(
        rows,
        schema={
            "snapshot_utc": UTC_US,
            "last_update_utc": UTC_US,
            "event_id": pl.String,
            "commence_time_utc": UTC_US,
            "home": pl.String,
            "away": pl.String,
            "book": pl.String,
            "market": pl.String,
            "side": pl.String,
            "line": pl.Float64,
            "price_decimal": pl.Float64,
            "is_closing_proxy": pl.Boolean,
            "slot": pl.String,
            "raw_key": pl.String,
            "snapshot_date": pl.Date,
            "game_id": pl.Int64,
            "game_type": pl.Int8,
        },
    )


DEPTH_SCHEMA = {
    "observed_utc": UTC_US,
    "season": pl.Int32,
    "game_date": pl.Date,
    "game_id": pl.Int64,
    "start_utc": UTC_US,
    "team": pl.String,
    "ticker": pl.String,
    "side": pl.String,
    "price": pl.Float64,
    "quantity": pl.Float64,
    "raw_key": pl.String,
}


def depth(
    books: Sequence[
        tuple[datetime, str, Sequence[tuple[float, float]], Sequence[tuple[float, float]]]
    ],
) -> pl.DataFrame:
    """Each (observed, team, Yes asks, Yes bids) as Kalshi's book logs it: a Yes ask at q is a No
    bid at 1 - q."""
    rows = []
    for observed, team, asks, bids in books:
        base = {
            "observed_utc": observed,
            "season": SEASON,
            "game_date": DAY,
            "game_id": GAME,
            "start_utc": START,
            "team": team,
            "ticker": TICKERS[team],
            "raw_key": f"kalshi/watch/books/{observed:%Y%m%dT%H%M%SZ}",
        }
        for ask, quantity in asks:
            rows.append(base | {"side": "no", "price": round(1 - ask, 4), "quantity": quantity})
        for bid, quantity in bids:
            rows.append(base | {"side": "yes", "price": bid, "quantity": quantity})
    return pl.DataFrame(rows, schema=DEPTH_SCHEMA)


TRADE_SCHEMA = {
    "trade_id": pl.String,
    "observed_utc": UTC_US,
    "created_utc": UTC_US,
    "season": pl.Int32,
    "game_date": pl.Date,
    "game_id": pl.Int64,
    "start_utc": UTC_US,
    "team": pl.String,
    "ticker": pl.String,
    "yes_price": pl.Float64,
    "count": pl.Float64,
    "taker_side": pl.String,
    "is_block_trade": pl.Boolean,
    "raw_key": pl.String,
}


def trades(found: Sequence[tuple[datetime, str, float, float]] = ()) -> pl.DataFrame:
    """Each (created, team, Yes price, contracts) trade, fetched a minute later."""
    rows = [
        {
            "trade_id": f"t{i}",
            "observed_utc": created + timedelta(minutes=1),
            "created_utc": created,
            "season": SEASON,
            "game_date": DAY,
            "game_id": GAME,
            "start_utc": START,
            "team": team,
            "ticker": TICKERS[team],
            "yes_price": price,
            "count": count,
            "taker_side": "no",
            "is_block_trade": False,
            "raw_key": "kalshi/watch/trades/x",
        }
        for i, (created, team, price, count) in enumerate(found)
    ]
    return pl.DataFrame(rows, schema=TRADE_SCHEMA)


TOP_SCHEMA = {
    "observed_utc": UTC_US,
    "season": pl.Int32,
    "game_date": pl.Date,
    "game_id": pl.Int64,
    "start_utc": UTC_US,
    "team": pl.String,
    "ticker": pl.String,
    "status": pl.String,
    "yes_bid": pl.Float64,
    "yes_bid_size": pl.Float64,
    "yes_ask": pl.Float64,
    "yes_ask_size": pl.Float64,
    "last_price": pl.Float64,
    "volume": pl.Float64,
    "open_interest": pl.Float64,
    "raw_key": pl.String,
}


def tops(found: Sequence[tuple[datetime, str, float]] = ()) -> pl.DataFrame:
    """Each (observed, team, best Yes ask) top of book."""
    rows = [
        {
            "observed_utc": observed,
            "season": SEASON,
            "game_date": DAY,
            "game_id": GAME,
            "start_utc": START,
            "team": team,
            "ticker": TICKERS[team],
            "status": "active",
            "yes_bid": None,
            "yes_bid_size": None,
            "yes_ask": ask,
            "yes_ask_size": 10.0,
            "last_price": None,
            "volume": None,
            "open_interest": None,
            "raw_key": "kalshi/watch/tops/x",
        }
        for observed, team, ask in found
    ]
    return pl.DataFrame(rows, schema=TOP_SCHEMA)


def dailyfaceoff(polls: Sequence[tuple[datetime, str, str]] = ()) -> pl.DataFrame:
    """Each (observed, team, goalie) Daily Faceoff poll of the game."""
    rows = [
        {
            "game_date": DAY,
            "team": team,
            "observed_utc": observed,
            "goalie_name": goalie,
            "status": "Confirmed",
            "start_utc": START,
        }
        for observed, team, goalie in polls
    ]
    return pl.DataFrame(
        rows,
        schema={
            "game_date": pl.Date,
            "team": pl.String,
            "observed_utc": UTC_US,
            "goalie_name": pl.String,
            "status": pl.String,
            "start_utc": UTC_US,
        },
    )
