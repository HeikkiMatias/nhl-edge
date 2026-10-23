"""A Kalshi order's record with its fills, and Pinnacle's quotes around it, for #318's tests: WSH
(home) against PIT, starting at 19:30 ET, with its closing proxy at the 18:45 ET slot."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl
import predict_fixtures as pf

from nhl_edge.live import bundle as lb
from nhl_edge.live import placement as pm
from nhl_edge.market.devig import fair_probabilities

WSH = 2026020053
PLACED = pf.DECISION + timedelta(minutes=3)
PROXY = datetime(2026, 10, 7, 22, 45, tzinfo=UTC)
AFTER = pf.START + timedelta(hours=4)
CLOSE = (1.80, 2.10)
P_HOME = float(fair_probabilities([list(CLOSE)])[0, 0])
TEAMS = pl.DataFrame(
    {
        "game_id": list(pf.GAMES),
        "home": [home for home, _, _ in pf.GAMES.values()],
        "away": [away for _, away, _ in pf.GAMES.values()],
    }
)


def fill(at: datetime, count: str, price: str, fee: str, taker: bool | None) -> dict[str, Any]:
    """A fill as Kalshi lists it; taker None leaves is_taker out."""
    found: dict[str, Any] = {
        "count_fp": count,
        "yes_price_dollars": price,
        "fee_cost": fee,
        "created_time": at.isoformat().replace("+00:00", "Z"),
    }
    if taker is not None:
        found["is_taker"] = taker
    return found


def record(
    store: lb.Store,
    fills: list[dict[str, Any]],
    team: str = "WSH",
    env: str = pm.DEMO,
    game_id: int = WSH,
    day: Any = pf.DAY,
    placed_at: datetime = PLACED,
    adopted: bool = False,
    created: datetime | None = None,
) -> None:
    """An order's record as placement writes it: the order, its placed part (adopted when a later
    run found it on Kalshi) and its final one, with Kalshi's creation time when created is set."""
    order = {
        "game_date": day.isoformat(),
        "season": 20262027,
        "game_id": game_id,
        "team": team,
        "start_utc": pf.START.isoformat(),
        "written_utc": (PLACED - timedelta(seconds=30)).isoformat(),
    }
    placed: dict[str, Any] = {"order_id": f"order-{game_id}", "placed_utc": placed_at.isoformat()}
    if adopted:
        placed |= {"existed": True, "adopted": True}
    kalshi: dict[str, Any] = {"status": "executed"}
    if created is not None:
        kalshi["created_time"] = created.isoformat().replace("+00:00", "Z")
    final = pm.final_record(kalshi, fills, pf.START, AFTER)
    for part, body in (("", order), ("placed", placed), ("final", final)):
        lb.put_once(store, pm.order_key(env, day, game_id, part), json.dumps(body).encode())


def two_parts(
    store: lb.Store, taker: list[dict[str, Any]], maker: list[dict[str, Any]], env: str = pm.DEMO
) -> None:
    """An ADR 0045 order's record (#321): its taker part, sent at PLACED, and its maker part, each
    with its fills, and the final record placement writes from them."""
    order = {
        "execution": "adr-0045",
        "game_date": pf.DAY.isoformat(),
        "season": 20262027,
        "game_id": WSH,
        "team": "WSH",
        "start_utc": pf.START.isoformat(),
        "written_utc": (PLACED - timedelta(seconds=30)).isoformat(),
    }
    parts = {}
    for name, order_id, listed in (("taker", "order-t", taker), ("maker", "order-m", maker)):
        count = sum(float(f["count_fp"]) for f in listed)
        shown = {"order_id": order_id, "status": "canceled", "fill_count_fp": f"{count:.2f}"}
        parts[name] = (shown, [f | {"order_id": order_id} for f in listed])
    final = pm.final_parts(parts, pf.START, AFTER)
    placed = {"order_id": "order-t", "placed_utc": PLACED.isoformat()}
    maker_placed = {
        "order_id": "order-m",
        "placed_utc": (PLACED + timedelta(seconds=2)).isoformat(),
    }
    for part, body in (
        ("", order),
        (pm.TAKER_PLACED, placed),
        (pm.MAKER_PLACED, maker_placed),
        ("final", final),
    ):
        lb.put_once(store, pm.order_key(env, pf.DAY, WSH, part), json.dumps(body).encode())


def odds(proxy: datetime | None = PROXY, *later: datetime) -> pl.DataFrame:
    """Pinnacle's decision quote, its pre-game quote at proxy (none when None), and any later
    snapshots at the same prices."""
    rows = [pf.quote(pf.MIDDAY, WSH, "pinnacle", 2.0, 1.85)]
    if proxy is not None:
        rows.append(pf.quote(proxy, WSH, "pinnacle", *CLOSE, slot="pre7"))
    rows.extend(pf.quote(at, WSH, "pinnacle", *CLOSE, slot="pre8") for at in later)
    quotes = pf.quotes(*rows)
    return quotes.with_columns(game_id=pl.col("event_id").str.strip_prefix("e").cast(pl.Int64))
