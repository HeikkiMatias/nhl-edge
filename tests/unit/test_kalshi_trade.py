"""Kalshi's trading API client (#286) against a fake exchange (kalshi_exchange): no test calls
Kalshi, and every response here is synthetic (the public repository holds no Kalshi data,
Developer Agreement 3.1)."""

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from kalshi_exchange import ED25519, KEY_ID, NOW, OTHER, RSA, TICKER, Exchange, pem, trade, verify
from typer.testing import CliRunner

from nhl_edge.cli import app
from nhl_edge.live import kalshi_trade as kt
from nhl_edge.live.kalshi_trade import (
    Credentials,
    KalshiTradeError,
    OrderRequest,
    check,
    check_market,
    load_key,
)
from nhl_edge.settings import MissingSettingError


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


@pytest.mark.parametrize("key", [ED25519, RSA], ids=["ed25519", "rsa"])
@pytest.mark.parametrize("join", ["\n", "", " "], ids=["lines", "one line", "spaces"])
def test_a_key_loads_from_its_base64_text_without_the_begin_and_end_lines(
    key: Any, join: str
) -> None:
    # #300: the key's text pasted without its first and last lines, in Kalshi's own formats.
    if isinstance(key, rsa.RSAPrivateKey):
        text = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode()
    else:
        text = pem(key)
    body = join.join(text.strip().splitlines()[1:-1])
    loaded = load_key(body)
    assert loaded.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ) == key.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def test_a_key_that_is_no_pem_is_refused_without_echoing_it() -> None:
    with pytest.raises(KalshiTradeError, match="KALSHI_PRIVATE_KEY is no unencrypted") as raised:
        load_key("not-a-key-0123456789", "KALSHI_PRIVATE_KEY")
    assert "0123456789" not in str(raised.value)
    assert "it doesn't parse (20 characters on 1 lines, no BEGIN line)" in str(raised.value)
    encrypted = ED25519.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"secret"),
    ).decode()
    with pytest.raises(KalshiTradeError, match="it is encrypted"):
        load_key(encrypted)


def test_a_key_that_cant_be_read_is_named_by_its_shape_never_its_text() -> None:
    # #300: what was pasted in the private key's place, so the owner knows what to fix.
    public = (
        ED25519.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    lines = pem(RSA).strip().splitlines()
    cut = "\n".join(lines[: len(lines) // 2])
    shapes = {
        public: "it is a public key, and the private key goes here",
        "3f1c9a2e-5b7d-4e8f-9a01-23456789abcd": "it is shaped as a key id",
        cut: "it has its BEGIN line but no END line, as if cut off",
        pem(ED25519).replace("A", "*"): "it doesn't parse",
    }
    for text, shape in shapes.items():
        with pytest.raises(KalshiTradeError, match=shape) as raised:
            load_key(text, "KALSHI_DEMO_PRIVATE_KEY")
        message = str(raised.value)
        for line in text.splitlines():
            if not line.startswith("-----"):
                assert line[4:20] not in message


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
        "1.50",
        True,
    )
    assert body["expiration_time"] == int((NOW + kt.CHECK_LIFE).timestamp())
    assert placed["status"] == "canceled"
    printed = "\n".join(lines)
    for secret in (TICKER, "order-1", "5000", KEY_ID, "0.61"):
        assert secret not in printed


def test_the_check_waits_a_little_for_its_writes_to_show(tmp_path: Path) -> None:
    exchange = Exchange()
    exchange.unseen = 2  # the new order is missing from the first two reads
    exchange.stale = 1  # and the first read after the cancel still shows it resting
    client, waits = trade(tmp_path, exchange)
    report = check(client, lambda _: None)
    assert report.passed
    assert waits == [kt.READ_WAIT_S] * 3


def test_an_order_that_never_shows_fails_the_check_and_is_still_cancelled(tmp_path: Path) -> None:
    exchange = Exchange()
    exchange.unseen = kt.READ_TRIES
    client, _ = trade(tmp_path, exchange)
    report = check(client, lambda _: None)
    assert not report.resting and not report.passed
    assert exchange.orders["order-1"]["status"] == "canceled"


def test_a_partial_fill_fails_the_check(tmp_path: Path) -> None:
    exchange = Exchange()
    exchange.filled_before_cancel = "0.50"
    client, _ = trade(tmp_path, exchange)
    lines: list[str] = []
    report = check(client, lines.append)
    assert report.cancelled and report.filled == Decimal("0.50") and not report.passed
    assert "0.50 contracts filled" in lines[-1]


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
    real_read: Callable[..., dict[str, Any] | None] = client.read_order
    calls = {"n": 0}

    def failing(order_id: str, until: Any) -> dict[str, Any] | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KalshiTradeError("order: status 500")
        return real_read(order_id, until)

    client.read_order = failing  # type: ignore[method-assign]
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


def test_a_refusal_carries_kalshis_error_code_for_the_private_record() -> None:
    # #346: the message stays the call and status (run logs are public); the code rides apart.
    explained = httpx.Response(403, json={"error": {"code": "not_allowed", "message": "no"}})
    assert kt.error_code(explained) == "not_allowed"
    assert kt.error_code(httpx.Response(403, json={"code": "other_shape"})) is None
    assert kt.error_code(httpx.Response(403, text="<html>")) is None
    assert kt.error_code(httpx.Response(403, json=["a list"])) is None
