"""The synthetic real day's two-part orders placed on the fake exchange, with what re-values them
(#321): the day's ledger in the record, Odds API bodies, Daily Faceoff pages and the watch's top
of book polls stored raw. Every response is SYNTHETIC; no test calls Kalshi, the Odds API or
Daily Faceoff."""

import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import predict_fixtures as pf
import real_fixtures as rf
from fakes import ConditionalBucket
from kalshi_exchange import Exchange, trade

from nhl_edge.ingest import kalshi
from nhl_edge.ingest import kalshi_watch as kw
from nhl_edge.ingest.odds import BASE_URL, OddsApi
from nhl_edge.lake.raw import RawStore
from nhl_edge.live import bundle as lb
from nhl_edge.live import kalshi_trade as kt
from nhl_edge.live import placement as pm
from nhl_edge.live import real
from nhl_edge.live import real_ledger as rl
from nhl_edge.live.predict import ledger_key

KALSHI = real.Venue("kalshi", 0.0, 0.01, real.Exchange("live", 1.16))
REAL_DAY = rl.Mode(rl.REAL, rf.ALERT.date())
PLACED_AT = rf.ALERT + timedelta(minutes=3)  # 16:55 UTC
SLOT = datetime(2026, 10, 7, 22, 45, 30, tzinfo=UTC)  # the pre7 slot, 18:45 ET
STARTS = {game_id: game[2] for game_id, game in pf.GAMES.items()}
WSH = "KXNHLGAME-26OCT07PITWSH-WSH"
WSH_MAKER = "order-2"  # with no asks: each bet's taker part, then its maker part
NAMES = {
    "WSH": "Washington Capitals",
    "PIT": "Pittsburgh Penguins",
    "WPG": "Winnipeg Jets",
    "COL": "Colorado Avalanche",
    "ANA": "Anaheim Ducks",
    "EDM": "Edmonton Oilers",
}
DECISION_PRICES = {2026020053: (2.10, 1.80), 2026020054: (1.90, 2.00), 2026020055: (2.5, 1.6)}


class Clock:
    def __init__(self, at: datetime = PLACED_AT) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def rows() -> Any:
    return rf.alert_rows(REAL_DAY, venue=KALSHI)


def parquet(frame: Any) -> bytes:
    buffer = io.BytesIO()
    frame.write_parquet(buffer)
    return buffer.getvalue()


def placed(
    tmp_path: Path, exchange: Exchange | None = None
) -> tuple[lb.R2Store, RawStore, Exchange, kt.KalshiTrade, Clock]:
    """The day's live orders placed at PLACED_AT, each maker part resting, with the day's ledger
    in the record and an empty raw store."""
    record, exchange, clock = (
        lb.R2Store(ConditionalBucket(), "bucket"),
        exchange or Exchange(),
        Clock(),
    )
    exchange.now = clock
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    report = pm.place(record, client, pm.plans(rows(), pm.LIVE), clock, rf.CODE, STARTS)
    assert report.makers == 3
    lb.put_once(record, ledger_key(pf.DAY), parquet(rf.ledger()))
    return record, RawStore(tmp_path / "raw"), exchange, client, clock


def odds_body(
    prices: dict[int, tuple[float, float]],
    updated: datetime,
    book: str = "pinnacle",
    commence: datetime | None = None,
) -> bytes:
    """SYNTHETIC: an Odds API h2h response for the day's games at these home and away prices,
    each starting at the schedule's start unless commence is given."""
    events = []
    for game_id, (home_price, away_price) in prices.items():
        home, away, start = pf.GAMES[game_id]
        start = commence or start
        market = {
            "key": "h2h",
            "last_update": updated.isoformat().replace("+00:00", "Z"),
            "outcomes": [
                {"name": NAMES[home], "price": home_price},
                {"name": NAMES[away], "price": away_price},
            ],
        }
        events.append(
            {
                "id": f"e{game_id}",
                "home_team": NAMES[home],
                "away_team": NAMES[away],
                "commence_time": start.isoformat().replace("+00:00", "Z"),
                "bookmakers": [
                    {
                        "key": book,
                        "last_update": updated.isoformat().replace("+00:00", "Z"),
                        "markets": [market],
                    }
                ],
            }
        )
    return json.dumps(events).encode()


def odds_api(body: bytes, clock: Clock, calls: list[httpx.Request]) -> OddsApi:
    """An Odds API client answering every request with body, each request kept."""

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=body, headers={"x-requests-remaining": "400"})

    client = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(answer))
    return OddsApi("synthetic-key", client, now=clock)


def dfo_page(goalies: dict[str, int | None]) -> bytes:
    """SYNTHETIC: a Daily Faceoff starting-goalies page naming each team's goalie id (None for
    none), for the day's games."""
    games = []
    for home, away, start in pf.GAMES.values():
        game: dict[str, Any] = {
            "date": pf.DAY.isoformat(),
            "dateGmt": start.isoformat().replace("+00:00", "Z"),
            "homeTeamName": NAMES[home],
            "awayTeamName": NAMES[away],
        }
        for side, team in (("home", home), ("away", away)):
            goalie = goalies.get(team)
            game[f"{side}GoalieName"] = f"Goalie {goalie}" if goalie is not None else None
            game[f"{side}GoalieId"] = goalie
            game[f"{side}NewsStrengthName"] = "Likely" if goalie is not None else None
        games.append(game)
    data = json.dumps({"props": {"pageProps": {"data": games}}})
    return f'<script id="__NEXT_DATA__" type="application/json">{data}</script>'.encode()


def store_page(raw: RawStore, at: datetime, goalies: dict[str, int | None]) -> str:
    return raw.put(
        "dailyfaceoff",
        f"starting-goalies/{pf.DAY.isoformat()}/{at:%Y%m%dT%H%M%SZ}",
        dfo_page(goalies),
        {"fetched_utc": at.isoformat(), "status": 200},
    )


def store_tops(raw: RawStore, at: datetime, tops: dict[str, tuple[str, str]]) -> str | None:
    """A watch poll of the day's markets at at, each ticker with its best Yes bid and ask."""
    markets = kw.markets_of(pf.slate())
    listed = [
        {
            "ticker": ticker,
            "status": "active",
            "yes_bid_dollars": bid,
            "yes_ask_dollars": ask,
        }
        for ticker, (bid, ask) in tops.items()
    ]
    response = kw.Response(None, kalshi.OK, json.dumps({"markets": listed}).encode(), at, 1)
    return kw.store_poll(raw, kw.TOPS, markets, [response], at)


def store_slot(raw: RawStore, at: datetime, body: bytes, name: str = "pre7") -> str:
    return raw.put(
        "odds",
        f"{at:%Y-%m-%d}/{at:%Y%m%dT%H%M%SZ}_{name}_eu",
        body,
        {"fetched_utc": at.isoformat(), "slot": name},
    )


def records(record: lb.R2Store, game_id: int = 2026020053) -> list[dict[str, Any]]:
    """A game's valuations, oldest first."""
    found = pm.stored_orders(record, pm.LIVE)[(pf.DAY.isoformat(), game_id)]
    return pm.valuations(found)


def ids(found: Sequence[dict[str, Any]]) -> list[str]:
    return [r["order_id"] for r in found]
