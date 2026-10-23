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
) -> None:
    """An order's record as placement writes it: the order, its placed part and its final one."""
    order = {
        "game_date": day.isoformat(),
        "season": 20262027,
        "game_id": game_id,
        "team": team,
        "start_utc": pf.START.isoformat(),
        "written_utc": (PLACED - timedelta(seconds=30)).isoformat(),
    }
    placed = {"order_id": f"order-{game_id}", "placed_utc": PLACED.isoformat()}
    final = pm.final_record({"status": "executed"}, fills, pf.START, AFTER)
    for part, body in (("", order), ("placed", placed), ("final", final)):
        lb.put_once(store, pm.order_key(env, day, game_id, part), json.dumps(body).encode())


def odds(proxy: datetime | None = PROXY, *later: datetime) -> pl.DataFrame:
    """Pinnacle's decision quote, its pre-game quote at proxy (none when None), and any later
    snapshots at the same prices."""
    rows = [pf.quote(pf.MIDDAY, WSH, "pinnacle", 2.0, 1.85)]
    if proxy is not None:
        rows.append(pf.quote(proxy, WSH, "pinnacle", *CLOSE, slot="pre7"))
    rows.extend(pf.quote(at, WSH, "pinnacle", *CLOSE, slot="pre8") for at in later)
    quotes = pf.quotes(*rows)
    return quotes.with_columns(game_id=pl.col("event_id").str.strip_prefix("e").cast(pl.Int64))
