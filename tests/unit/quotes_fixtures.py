"""SYNTHETIC logs for shadow kalshi_quotes' tests (#358): one game, its decision, Pinnacle's pair
and made-up Kalshi tops, books and trades. Every number is made up, since the public repository
holds no Kalshi response (Developer Agreement 3.1)."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import polars as pl

DAY = date(2026, 11, 3)
GAME = 2026020150
HOME, AWAY = "WSH", "PIT"
START = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)  # 19:00 EST
PUBLISHED = datetime(2026, 11, 3, 17, 50, tzinfo=UTC)
FIRST_TOP = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
UTC_TYPE = pl.Datetime("us", "UTC")


def at(minutes: float) -> datetime:
    return FIRST_TOP + timedelta(minutes=minutes)


def ledger(p_blend: float = 0.55, published: datetime = PUBLISHED, start: datetime = START):
    return pl.DataFrame(
        {
            "game_id": [GAME],
            "season": [20262027],
            "game_date": [DAY],
            "start_utc": [start],
            "home": [HOME],
            "away": [AWAY],
            "status": ["predicted"],
            "p_blend": [p_blend],
            "published_utc": [published],
        }
    ).with_columns(
        pl.col("season").cast(pl.Int32), pl.col("start_utc", "published_utc").cast(UTC_TYPE)
    )


def morning(rows: list[tuple[float, datetime]] = ()) -> pl.DataFrame:  # type: ignore[assignment]
    return pl.DataFrame(
        {
            "game_id": [GAME] * len(rows),
            "game_date": [DAY] * len(rows),
            "status": ["predicted"] * len(rows),
            "p_blend": [p for p, _ in rows],
            "published_utc": [t for _, t in rows],
        },
        schema={
            "game_id": pl.Int64,
            "game_date": pl.Date,
            "status": pl.String,
            "p_blend": pl.Float64,
            "published_utc": UTC_TYPE,
        },
    )


def tops(
    minutes: int = 41,
    home: tuple[float, float] = (0.50, 0.54),
    away: tuple[float, float] = (0.44, 0.48),
    moved: dict[int, tuple[float, float]] | None = None,
) -> pl.DataFrame:
    """SYNTHETIC: both markets' tops every minute from FIRST_TOP; moved gives the home market's
    (bid, ask) from a minute on."""
    rows = []
    for m in range(minutes):
        h = home
        for start, value in sorted((moved or {}).items()):
            if m >= start:
                h = value
        for team, (bid, ask) in ((HOME, h), (AWAY, away)):
            rows.append(
                {
                    "game_id": GAME,
                    "game_date": DAY,
                    "team": team,
                    "observed_utc": at(m),
                    "yes_bid": bid,
                    "yes_ask": ask,
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("observed_utc").cast(UTC_TYPE))


def depth(minutes: int = 41, home_bids: dict[float, float] | None = None) -> pl.DataFrame:
    """SYNTHETIC: both markets' books every 5 minutes: 5 contracts bid at 0.53 for the home team
    by default, and the asks as the tops give them (a No bid at 1 - ask)."""
    rows = []
    for m in range(0, minutes, 5):
        for team, bids, ask in (
            (HOME, home_bids if home_bids is not None else {0.53: 5.0}, 0.54),
            (AWAY, {0.40: 5.0}, 0.48),
        ):
            for price, quantity in bids.items():
                rows.append(_level(team, at(m), "yes", price, quantity))
            rows.append(_level(team, at(m), "no", round(1 - ask, 2), 50.0))
    return pl.DataFrame(rows).with_columns(pl.col("observed_utc").cast(UTC_TYPE))


def _level(team: str, when: datetime, side: str, price: float, quantity: float) -> dict:
    return {
        "game_id": GAME,
        "game_date": DAY,
        "team": team,
        "observed_utc": when,
        "side": side,
        "price": price,
        "quantity": quantity,
    }


def trades(rows: Sequence[tuple[float, str, float, float]] = ()) -> pl.DataFrame:
    """SYNTHETIC: (minute, team, yes price, contracts) trades."""
    return pl.DataFrame(
        {
            "game_id": [GAME] * len(rows),
            "game_date": [DAY] * len(rows),
            "team": [team for _, team, _, _ in rows],
            "created_utc": [at(m) for m, _, _, _ in rows],
            "yes_price": [p for _, _, p, _ in rows],
            "count": [c for _, _, _, c in rows],
        },
        schema={
            "game_id": pl.Int64,
            "game_date": pl.Date,
            "team": pl.String,
            "created_utc": UTC_TYPE,
            "yes_price": pl.Float64,
            "count": pl.Float64,
        },
    )


def odds(pairs: list[tuple[datetime, float, float]] = ()) -> pl.DataFrame:  # type: ignore[assignment]
    """Pinnacle's h2h pairs (snapshot, home price, away price), each fresh at its snapshot."""
    rows = []
    for snapshot, home_price, away_price in pairs:
        for side, price in (("home", home_price), ("away", away_price)):
            rows.append(
                {
                    "book": "pinnacle",
                    "market": "h2h",
                    "game_id": GAME,
                    "event_id": "e1",
                    "snapshot_utc": snapshot,
                    "last_update_utc": snapshot - timedelta(seconds=30),
                    "commence_time_utc": START,
                    "home": HOME,
                    "away": AWAY,
                    "side": side,
                    "price_decimal": price,
                    "slot": "midday",
                    "is_closing_proxy": False,
                }
            )
    schema = {
        "book": pl.String, "market": pl.String, "game_id": pl.Int64, "event_id": pl.String,
        "snapshot_utc": UTC_TYPE, "last_update_utc": UTC_TYPE, "commence_time_utc": UTC_TYPE,
        "home": pl.String, "away": pl.String, "side": pl.String, "price_decimal": pl.Float64,
        "slot": pl.String, "is_closing_proxy": pl.Boolean,
    }  # fmt: skip
    return pl.DataFrame(rows, schema=schema)


def dailyfaceoff(polls: list[tuple[datetime, str, str]] = ()) -> pl.DataFrame:  # type: ignore[assignment]
    """Daily Faceoff's polls (observed, team, goalie name) of the game's listing."""
    return pl.DataFrame(
        {
            "game_date": [DAY] * len(polls),
            "team": [team for _, team, _ in polls],
            "start_utc": [START] * len(polls),
            "goalie_name": [name for _, _, name in polls],
            "observed_utc": [t for t, _, _ in polls],
        },
        schema={
            "game_date": pl.Date,
            "team": pl.String,
            "start_utc": UTC_TYPE,
            "goalie_name": pl.String,
            "observed_utc": UTC_TYPE,
        },
    )


def games(home_win: bool = True) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "game_id": [GAME],
            "home": [HOME],
            "away": [AWAY],
            "home_score": [3 if home_win else 1],
            "away_score": [1 if home_win else 3],
            "observed_utc": [START + timedelta(hours=12)],
        }
    ).with_columns(pl.col("observed_utc").cast(UTC_TYPE))
