"""Kalshi's trading API client (#286) against a fake exchange: no test calls Kalshi, and every
response here is synthetic (the public repository holds no Kalshi data, Developer Agreement 3.1)."""

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from typer.testing import CliRunner

from nhl_edge.cli import app
from nhl_edge.lake.raw import RawStore
from nhl_edge.live import kalshi_trade as kt
from nhl_edge.live.kalshi_trade import (
    Credentials,
    KalshiTrade,
    KalshiTradeError,
    OrderRequest,
    check,
    check_market,
    load_key,
)
from nhl_edge.settings import MissingSettingError

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


class Exchange:
    """A fake of the trading API's calls used here, which checks each request's signature."""

    def __init__(self, key: Any = ED25519) -> None:
        self.key = key
        self.requests: list[httpx.Request] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.markets = [
            {"ticker": TICKER, "yes_ask_dollars": "0.6100"},
            {"ticker": OTHER, "yes_ask_dollars": "0.4100"},
        ]
        # status codes to answer before the route, one per request
        self.fail: list[int] = []
        # the next create reaches the exchange but answers this status
        self.lost_create: int | None = None

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
            return httpx.Response(200, json={"balance": 5000, "portfolio_value": 0})
        if path == "/markets":
            return httpx.Response(200, json={"markets": self.markets, "cursor": ""})
        if path == "/portfolio/events/orders" and request.method == "POST":
            return self._create(json.loads(request.content))
        if path.startswith("/portfolio/events/orders/") and request.method == "DELETE":
            order = self.orders.get(path.rsplit("/", 1)[1])
            if order is None or order["status"] != "resting":
                return httpx.Response(404, json={"code": "not_found"})
            order["status"] = "canceled"
            return httpx.Response(200, json={"order_id": order["order_id"], "reduced_by": "1.00"})
        if path.startswith("/portfolio/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[1])
            if order is None:
                return httpx.Response(404, json={"code": "not_found"})
            return httpx.Response(200, json={"order": order})
        if path == "/portfolio/orders":
            listed = [o for o in self.orders.values() if o["ticker"] == params["ticker"]]
            return httpx.Response(200, json={"orders": listed, "cursor": ""})
        if path == "/portfolio/fills":
            return httpx.Response(200, json={"fills": [], "cursor": ""})
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


def trade(
    tmp_path: Path, exchange: Exchange, key: Any = ED25519, env: str = kt.DEMO
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
            now=lambda: NOW,
            min_interval_s=0.0,
            sleep=waits.append,
        ),
        waits,
    )


def order(**changes: Any) -> OrderRequest:
    fields: dict[str, Any] = {
        "ticker": TICKER,
        "client_order_id": "real-2026-10-18-2026020101-v2",
        "count": Decimal("2.5"),
        "price": Decimal("0.77"),
        "expiration_time": NOW + timedelta(hours=6),
    }
    return OrderRequest(**(fields | changes))


# Signing


@pytest.mark.parametrize("key", [ED25519, RSA], ids=["ed25519", "rsa"])
def test_the_signature_covers_the_time_the_method_and_the_path(key: Any) -> None:
    headers = Credentials(KEY_ID, key).headers("GET", "/trade-api/v2/portfolio/balance", NOW)
    assert headers["KALSHI-ACCESS-KEY"] == KEY_ID
    stamp = headers["KALSHI-ACCESS-TIMESTAMP"]
    assert stamp == str(int(NOW.timestamp() * 1000))
    verify(key, headers["KALSHI-ACCESS-SIGNATURE"], f"{stamp}GET/trade-api/v2/portfolio/balance")
    with pytest.raises(InvalidSignature):
        verify(
            key, headers["KALSHI-ACCESS-SIGNATURE"], f"{stamp}POST/trade-api/v2/portfolio/balance"
        )


def test_a_request_signs_its_path_from_the_host_without_the_query(tmp_path: Path) -> None:
    exchange = Exchange()
    client, _ = trade(tmp_path, exchange)
    client.orders(TICKER)
    sent = exchange.requests[0]
    assert sent.url.path == "/trade-api/v2/portfolio/orders"
    assert sent.url.params["ticker"] == TICKER and sent.url.params["limit"] == "100"


def test_an_rsa_key_signs_requests_too(tmp_path: Path) -> None:
    exchange = Exchange(RSA)
    client, _ = trade(tmp_path, exchange, RSA)
    assert client.balance()["balance"] == 5000


def test_a_key_loads_from_its_pem_text_or_from_one_line_with_escaped_newlines() -> None:
    text = pem(ED25519)
    assert isinstance(load_key(text), ed25519.Ed25519PrivateKey)
    assert isinstance(load_key(text.replace("\n", "\\n")), ed25519.Ed25519PrivateKey)
    assert isinstance(load_key(pem(RSA)), rsa.RSAPrivateKey)


def test_a_key_that_is_no_pem_is_refused_without_echoing_it() -> None:
    with pytest.raises(KalshiTradeError, match="KALSHI_PRIVATE_KEY is no unencrypted") as raised:
        load_key("not-a-key-0123456789", "KALSHI_PRIVATE_KEY")
    assert "0123456789" not in str(raised.value)
    encrypted = ED25519.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"secret"),
    ).decode()
    with pytest.raises(KalshiTradeError):
        load_key(encrypted)


def test_credentials_come_from_each_environments_variables_and_never_show(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KALSHI_DEMO_KEY_ID", KEY_ID)
    monkeypatch.setenv("KALSHI_DEMO_PRIVATE_KEY", pem(ED25519))
    monkeypatch.delenv("KALSHI_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY", raising=False)
    found = Credentials.from_env(kt.DEMO)
    assert found.key_id == KEY_ID and KEY_ID not in repr(found)
    with pytest.raises(MissingSettingError, match="KALSHI_KEY_ID"):
        Credentials.from_env(kt.LIVE)


# Requests


def test_a_rate_limit_or_server_error_is_tried_again_with_growing_waits(tmp_path: Path) -> None:
    exchange = Exchange()
    exchange.fail = [429, 503]
    client, waits = trade(tmp_path, exchange)
    ticks = iter(NOW + timedelta(seconds=i) for i in range(100))
    client.now = lambda: next(ticks)
    assert client.balance()["balance"] == 5000
    assert len(exchange.requests) == 3 and waits == [1.0, 2.0]
    stamps = {r.headers["KALSHI-ACCESS-TIMESTAMP"] for r in exchange.requests}
    assert len(stamps) == 3  # each try signed afresh, at its own time


def test_a_call_gives_up_after_its_tries(tmp_path: Path) -> None:
    exchange = Exchange()
    exchange.fail = [500] * 5
    client, waits = trade(tmp_path, exchange)
    with pytest.raises(KalshiTradeError, match="balance: failed after 5 tries"):
        client.balance()
    assert len(exchange.requests) == 5 and waits == [1.0, 2.0, 4.0, 8.0]


def test_another_status_is_an_error_at_once(tmp_path: Path) -> None:
    exchange = Exchange()
    exchange.fail = [400]
    client, _ = trade(tmp_path, exchange)
    with pytest.raises(KalshiTradeError, match="create-order: status 400"):
        client.create_order(order())
    assert len(exchange.requests) == 1 and not exchange.orders


def test_a_transport_error_is_tried_again(tmp_path: Path) -> None:
    exchange = Exchange()
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("synthetic")
        return exchange(request)

    client, waits = trade(tmp_path, exchange)
    client.client = httpx.Client(
        base_url=kt.HOSTS[kt.DEMO] + kt.API_ROOT, transport=httpx.MockTransport(flaky)
    )
    assert client.balance()["balance"] == 5000 and waits == [1.0]


def test_a_listing_follows_its_cursor(tmp_path: Path) -> None:
    pages = [
        {"markets": [{"ticker": TICKER}], "cursor": "next"},
        {"markets": [{"ticker": OTHER}], "cursor": ""},
    ]
    seen: list[httpx.Request] = []

    def paged(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=pages[len(seen) - 1])

    client, _ = trade(tmp_path, Exchange())
    client.client = httpx.Client(
        base_url=kt.HOSTS[kt.DEMO] + kt.API_ROOT, transport=httpx.MockTransport(paged)
    )
    assert [m["ticker"] for m in client.markets()] == [TICKER, OTHER]
    assert "cursor" not in seen[0].url.params and seen[1].url.params["cursor"] == "next"
    assert seen[0].url.params["series_ticker"] == "KXNHLGAME"


# Orders


def test_an_order_is_a_limit_bid_good_until_its_expiry_and_cancelled_on_a_pause(
    tmp_path: Path,
) -> None:
    exchange = Exchange()
    client, _ = trade(tmp_path, exchange)
    placed = client.create_order(order())
    assert not placed.existed and placed.order_id == "order-1"
    assert exchange.orders["order-1"]["body"] == {
        "ticker": TICKER,
        "client_order_id": "real-2026-10-18-2026020101-v2",
        "side": "bid",
        "count": "2.50",
        "price": "0.7700",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "cancel_order_on_pause": True,
        "expiration_time": int((NOW + timedelta(hours=6)).timestamp()),
    }


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"count": Decimal("0.005")}, "count"),
        ({"count": Decimal("1.234")}, "count"),
        ({"price": Decimal("1")}, "price"),
        ({"price": Decimal("0")}, "price"),
        ({"price": Decimal("0.77001")}, "price"),
        ({"expiration_time": datetime(2026, 10, 18, 23, 0)}, "time zone"),
        ({"client_order_id": ""}, "client order id"),
    ],
)
def test_an_order_off_the_grid_is_refused_before_it_is_sent(
    changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        order(**changes)


def test_a_repeated_client_order_id_reads_the_existing_order_back(tmp_path: Path) -> None:
    exchange = Exchange()
    client, _ = trade(tmp_path, exchange)
    first = client.create_order(order())
    again = client.create_order(order())
    assert again.existed and again.order_id == first.order_id
    assert len(exchange.orders) == 1


def test_a_create_retried_after_it_reached_the_exchange_is_one_order(tmp_path: Path) -> None:
    exchange = Exchange()
    exchange.lost_create = 502
    client, _ = trade(tmp_path, exchange)
    placed = client.create_order(order())
    assert placed.existed and placed.order_id == "order-1"
    assert len(exchange.orders) == 1
    creates = [r for r in exchange.requests if r.method == "POST"]
    assert len(creates) == 2 and creates[0].content == creates[1].content


def test_a_409_without_its_order_is_an_error(tmp_path: Path) -> None:
    exchange = Exchange()
    exchange.fail = [409]
    client, _ = trade(tmp_path, exchange)
    with pytest.raises(KalshiTradeError, match="409, and no order has its id"):
        client.create_order(order())


def test_a_created_order_without_its_id_is_an_error(tmp_path: Path) -> None:
    client, _ = trade(tmp_path, Exchange())
    client.client = httpx.Client(
        base_url=kt.HOSTS[kt.DEMO] + kt.API_ROOT,
        transport=httpx.MockTransport(lambda _: httpx.Response(201, json={"ts_ms": 0})),
    )
    with pytest.raises(KalshiTradeError, match="201 without an order id"):
        client.create_order(order())


def test_cancelling_an_order_no_longer_resting_is_none(tmp_path: Path) -> None:
    exchange = Exchange()
    client, _ = trade(tmp_path, exchange)
    placed = client.create_order(order())
    assert client.cancel(placed.order_id, TICKER) is not None
    assert client.order(placed.order_id)["status"] == "canceled"
    assert client.cancel(placed.order_id, TICKER) is None
    deleted = [r for r in exchange.requests if r.method == "DELETE"]
    assert deleted[0].url.params["market_ticker"] == TICKER


# The record


def test_every_response_is_stored_raw_without_a_header(tmp_path: Path) -> None:
    exchange = Exchange()
    exchange.fail = [429]
    client, _ = trade(tmp_path, exchange)
    client.create_order(order())
    root = tmp_path / "raw" / "kalshi" / "trade" / "demo" / "2026-10-18"
    sidecars = sorted(root.glob("*.meta.json"))
    assert len(sidecars) == 2
    metas = [json.loads(path.read_text()) for path in sidecars]
    assert [(m["call"], m["status"], m["attempt"]) for m in metas] == [
        ("create-order", 429, 1),
        ("create-order", 201, 2),
    ]
    assert metas[1]["sent"]["client_order_id"] == "real-2026-10-18-2026020101-v2"
    assert metas[1]["path"] == "/trade-api/v2/portfolio/events/orders"
    stored = "".join(path.read_text() for path in sidecars)
    assert "KALSHI-ACCESS" not in stored and KEY_ID not in stored


# The check


def test_the_check_places_a_post_only_cent_bid_where_it_cannot_fill_and_cancels_it(
    tmp_path: Path,
) -> None:
    exchange = Exchange()
    client, _ = trade(tmp_path, exchange)
    lines: list[str] = []
    report = check(client, lines.append)
    assert report.passed and report.filled == 0
    (placed,) = exchange.orders.values()
    body = placed["body"]
    assert (body["ticker"], body["price"], body["count"], body["post_only"]) == (
        TICKER,
        "0.0100",
        "1.00",
        True,
    )
    assert body["expiration_time"] == int((NOW + kt.CHECK_LIFE).timestamp())
    assert placed["status"] == "canceled"
    printed = "\n".join(lines)
    for secret in (TICKER, "order-1", "5000", KEY_ID, "0.61"):
        assert secret not in printed


def test_the_check_needs_a_market_whose_ask_is_far_above_a_cent() -> None:
    assert (
        check_market([{"ticker": TICKER, "yes_ask_dollars": "0.0900"}, {"ticker": OTHER}]) is None
    )
    assert check_market([{"ticker": TICKER, "yes_ask_dollars": "0.1000"}]) == TICKER


def test_the_check_without_a_market_places_nothing(tmp_path: Path) -> None:
    exchange = Exchange()
    exchange.markets = []
    client, _ = trade(tmp_path, exchange)
    report = check(client, lambda _: None)
    assert report.balance_read and not report.placed and not report.passed
    assert not exchange.orders


def test_the_check_cancels_even_when_reading_the_order_back_fails(tmp_path: Path) -> None:
    exchange = Exchange()
    client, _ = trade(tmp_path, exchange)
    real_order: Callable[[str], dict[str, Any]] = client.order
    calls = {"n": 0}

    def failing(order_id: str) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KalshiTradeError("order: status 500")
        return real_order(order_id)

    client.order = failing  # type: ignore[method-assign]
    with pytest.raises(KalshiTradeError):
        check(client, lambda _: None)
    assert exchange.orders["order-1"]["status"] == "canceled"


# The command


runner = CliRunner()


def test_nhl_kalshi_check_prints_its_steps_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exchange = Exchange()
    client, _ = trade(tmp_path, exchange)
    monkeypatch.setattr(kt.KalshiTrade, "from_env", classmethod(lambda cls, env, store: client))
    result = runner.invoke(app, ["kalshi", "check", "--env", "demo"])
    assert result.exit_code == 0, result.output
    assert "kalshi check demo: passed" in result.output
    for secret in (TICKER, "order-1", "5000", KEY_ID):
        assert secret not in result.output


def test_nhl_kalshi_check_without_its_key_fails_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("nhl_edge.settings.load_env", lambda: None)
    monkeypatch.delenv("KALSHI_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY", raising=False)
    result = runner.invoke(app, ["kalshi", "check", "--env", "live"])
    assert result.exit_code == 1
    assert "KALSHI_KEY_ID is not set" in result.output


def test_nhl_kalshi_check_takes_demo_or_live_only() -> None:
    result = runner.invoke(app, ["kalshi", "check", "--env", "prod"])
    assert result.exit_code == 2
