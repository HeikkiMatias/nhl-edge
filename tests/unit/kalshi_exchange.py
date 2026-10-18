"""A fake of Kalshi's trading API for the client's (#286) and placement's (#281) tests: it checks
each request's signature and keeps orders, fills and markets in memory. Every response is
synthetic; no test calls Kalshi (the public repository holds no Kalshi data, Developer Agreement
3.1)."""

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from nhl_edge.lake.raw import RawStore
from nhl_edge.live import kalshi_trade as kt
from nhl_edge.live.kalshi_trade import Credentials, KalshiTrade

NOW = datetime(2026, 10, 18, 17, 0, tzinfo=UTC)
KEY_ID = "test-key-id-5f1c"
TICKER = "KXNHLGAME-26OCT18CARCHI-CAR"
OTHER = "KXNHLGAME-26OCT18CARCHI-CHI"
ED25519 = ed25519.Ed25519PrivateKey.generate()
RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key: Any) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def verify(key: Any, signature: str, text: str) -> None:
    raw = base64.b64decode(signature)
    if isinstance(key, ed25519.Ed25519PrivateKey):
        key.public_key().verify(raw, text.encode())
    else:
        key.public_key().verify(
            raw,
            text.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )


def dollars(value: Decimal) -> str:
    return f"{value:.6f}"


class Exchange:
    """A fake of the trading API's calls, which checks each request's signature."""

    def __init__(self, key: Any = ED25519) -> None:
        self.key = key
        self.requests: list[httpx.Request] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.fills: dict[str, list[dict[str, Any]]] = {}
        self.markets = [
            {"ticker": TICKER, "yes_ask_dollars": "0.6100"},
            {"ticker": OTHER, "yes_ask_dollars": "0.4100"},
        ]
        # a market's status by ticker, "active" when not set, and fields every market read
        # serves beside it, such as its prices
        self.status: dict[str, str] = {}
        self.market_fields: dict[str, Any] = {}
        self.markets_served: list[dict[str, Any]] = []
        self.balance_cents = 5000
        # status codes to answer before the route, one per request
        self.fail: list[int] = []
        # the next create reaches the exchange but answers this status
        self.lost_create: int | None = None
        # reads of an order that answer 404, or show it still resting after its cancel, as
        # Kalshi's reads lag its writes
        self.unseen = 0
        self.stale = 0
        # contracts a seller fills before the cancel lands
        self.filled_before_cancel = "0.00"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        stamp = request.headers["KALSHI-ACCESS-TIMESTAMP"]
        assert request.headers["KALSHI-ACCESS-KEY"] == KEY_ID
        verify(
            self.key,
            request.headers["KALSHI-ACCESS-SIGNATURE"],
            stamp + request.method + request.url.path,
        )
        if self.fail:
            return httpx.Response(self.fail.pop(0), json={"error": "synthetic"})
        path = request.url.path.removeprefix(kt.API_ROOT)
        params = request.url.params
        if path == "/portfolio/balance":
            return httpx.Response(200, json={"balance": self.balance_cents, "portfolio_value": 0})
        if path == "/markets":
            return httpx.Response(200, json={"markets": self.markets, "cursor": ""})
        if path.startswith("/markets/"):
            ticker = path.removeprefix("/markets/")
            market = {"ticker": ticker, "status": self.status.get(ticker, "active")}
            market |= self.market_fields
            self.markets_served.append(market)
            return httpx.Response(200, json={"market": market})
        if path == "/portfolio/events/orders" and request.method == "POST":
            return self._create(json.loads(request.content))
        if path.startswith("/portfolio/events/orders/") and request.method == "DELETE":
            order = self.orders.get(path.rsplit("/", 1)[1])
            if order is None or order["status"] != "resting":
                return httpx.Response(404, json={"code": "not_found"})
            order["status"] = "canceled"
            if self.filled_before_cancel != "0.00":
                order["fill_count_fp"] = self.filled_before_cancel
            return httpx.Response(200, json={"order_id": order["order_id"], "reduced_by": "1.00"})
        if path.startswith("/portfolio/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[1])
            if order is None or self.unseen:
                self.unseen = max(0, self.unseen - 1)
                return httpx.Response(404, json={"code": "not_found"})
            if order["status"] == "canceled" and self.stale:
                self.stale -= 1
                return httpx.Response(200, json={"order": order | {"status": "resting"}})
            return httpx.Response(200, json={"order": order})
        if path == "/portfolio/orders":
            listed = [o for o in self.orders.values() if o["ticker"] == params["ticker"]]
            return httpx.Response(200, json={"orders": listed, "cursor": ""})
        if path == "/portfolio/fills":
            listed = self.fills.get(params["order_id"], [])
            return httpx.Response(200, json={"fills": listed, "cursor": ""})
        return httpx.Response(404, json={"code": "no_route"})

    def _create(self, body: dict[str, Any]) -> httpx.Response:
        if any(o["client_order_id"] == body["client_order_id"] for o in self.orders.values()):
            return httpx.Response(409, json={"code": "order_already_exists"})
        order_id = f"order-{len(self.orders) + 1}"
        self.orders[order_id] = {
            "order_id": order_id,
            "client_order_id": body["client_order_id"],
            "ticker": body["ticker"],
            "status": "resting",
            "fill_count_fp": "0.00",
            "remaining_count_fp": body["count"],
            "taker_fill_cost_dollars": "0.000000",
            "maker_fill_cost_dollars": "0.000000",
            "taker_fees_dollars": "0.000000",
            "maker_fees_dollars": "0.000000",
            "body": body,
        }
        if self.lost_create is not None:
            status, self.lost_create = self.lost_create, None
            return httpx.Response(status, json={"error": "synthetic"})
        return httpx.Response(
            201,
            json={
                "order_id": order_id,
                "client_order_id": body["client_order_id"],
                "fill_count": "0.00",
                "remaining_count": body["count"],
                "ts_ms": 0,
            },
        )

    def fill(
        self,
        order_id: str,
        count: str,
        price: str,
        fee: str,
        at: datetime,
        taker: bool = False,
        record: bool = True,
    ) -> None:
        """A fill of part of an order; record=False leaves it out of the fills listing, as a read
        that lags the fill would."""
        order = self.orders[order_id]
        side = "taker" if taker else "maker"
        filled = Decimal(order["fill_count_fp"]) + Decimal(count)
        order["fill_count_fp"] = f"{filled:.2f}"
        order["remaining_count_fp"] = f"{Decimal(order['body']['count']) - filled:.2f}"
        cost_key, fee_key = f"{side}_fill_cost_dollars", f"{side}_fees_dollars"
        order[cost_key] = dollars(Decimal(order[cost_key]) + Decimal(count) * Decimal(price))
        order[fee_key] = dollars(Decimal(order[fee_key]) + Decimal(fee))
        if Decimal(order["remaining_count_fp"]) <= 0:
            order["status"] = "executed"
        if record:
            self.fills.setdefault(order_id, []).append(
                {
                    "fill_id": f"fill-{order_id}-{len(self.fills.get(order_id, []))}",
                    "order_id": order_id,
                    "ticker": order["ticker"],
                    "count_fp": count,
                    "yes_price_dollars": price,
                    "fee_cost": fee,
                    "is_taker": taker,
                    "created_time": at.isoformat().replace("+00:00", "Z"),
                }
            )

    def expire(self, order_id: str) -> None:
        """The order's expiry: its resting part cancelled by Kalshi."""
        if self.orders[order_id]["status"] == "resting":
            self.orders[order_id]["status"] = "canceled"


def trade(
    tmp_path: Path,
    exchange: Exchange,
    key: Any = ED25519,
    env: str = kt.DEMO,
    now: Callable[[], datetime] = lambda: NOW,
) -> tuple[KalshiTrade, list[float]]:
    waits: list[float] = []
    client = httpx.Client(
        base_url=kt.HOSTS[env] + kt.API_ROOT, transport=httpx.MockTransport(exchange)
    )
    return (
        KalshiTrade(
            env,
            Credentials(KEY_ID, key),
            RawStore(tmp_path / "raw"),
            client,
            now=now,
            min_interval_s=0.0,
            sleep=waits.append,
        ),
        waits,
    )
