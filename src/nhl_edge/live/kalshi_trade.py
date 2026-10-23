"""Kalshi's trading API (#286): requests signed with the owner's API key, for the owner's own
orders (#281). Kalshi's Developer Agreement allows its API only to facilitate a member's own
trading (3.1), which this is.

**Environments.** DEMO is Kalshi's demo exchange, with mock funds, and LIVE the real one. Each has
its own key: KALSHI_DEMO_KEY_ID and KALSHI_DEMO_PRIVATE_KEY, or KALSHI_KEY_ID and
KALSHI_PRIVATE_KEY (repository secrets, or .env on a laptop). The private key is PEM text.

**Signing.** Each request carries the key id, the time in milliseconds and a signature of the time,
the method and the path from the host (/trade-api/v2/...) without its query. An Ed25519 key signs
that text; an RSA key signs it with RSA-PSS over SHA-256 (MGF1 with SHA-256, the salt the digest's
length). The signature is base64 (docs.kalshi.com, API Keys).

**Requests.** At least MIN_INTERVAL_S apart. A 429, a 5xx or a transport error is tried again after
exponentially longer waits, at most TRIES tries (the agreement's 4.3), as ingest/kalshi.py does;
any other unexpected status is an error. An order is created with its client_order_id, so a retry
is the same order. Kalshi refuses a second order with that id with 409, which means an earlier try
or run created it, and the order is read back instead (Placed.existed).

**Record.** Every response is stored raw and untouched, under
kalshi/trade/<env>/<UTC date>/<stamp>_<request n>_<call>_<try>. Its sidecar holds the call, the
method, path and query, the body sent, the status and the try, and never a header, so never the
key or a signature.
Nothing is printed here: run logs are public, so prices, sizes, order ids and the balance stay in
the record, and an error names only its call and status.
"""

import base64
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from nhl_edge.ingest.kalshi import (
    BACKOFF_S,
    MAX_WAIT_S,
    MIN_INTERVAL_S,
    SERIES,
    SOURCE,
    TIMEOUT_S,
    retryable,
)
from nhl_edge.ingest.nhl_api import USER_AGENT, retry_after_s
from nhl_edge.lake.raw import RawStore
from nhl_edge.settings import require

DEMO, LIVE = "demo", "live"
ENVS = (DEMO, LIVE)
HOSTS = {DEMO: "https://external-api.demo.kalshi.co", LIVE: "https://external-api.kalshi.com"}
API_ROOT = "/trade-api/v2"
# Each environment's key id and private key, by the variables that hold them.
KEY_VARIABLES = {
    DEMO: ("KALSHI_DEMO_KEY_ID", "KALSHI_DEMO_PRIVATE_KEY"),
    LIVE: ("KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY"),
}
TRADE = "trade"
OK, CREATED, NOT_FOUND, CONFLICT = 200, 201, 404, 409
PAGE = 100
MAX_PAGES = 50
# How long a read waits for a write to show (read_order): at most 5 reads, a second apart.
READ_TRIES = 5
READ_WAIT_S = 1.0
CENT = Decimal("0.01")
# Create Order V2's times in force: good till cancelled, which an expiration time ends, and
# immediate or cancel.
GTC, IOC = "good_till_canceled", "immediate_or_cancel"
# The finest price and count an order takes: $0.0001 and 0.01 contracts (Fixed-Point
# Representation); a market's own grid can be coarser (its price_ranges).
PRICE_STEP = Decimal("0.0001")
COUNT_STEP = Decimal("0.01")

# A PEM block's label, and an API key id's shape: what key_shape may say of a key's text.
PEM_LABEL = re.compile(r"-----BEGIN ([A-Z0-9 ]{1,40})-----")
KEY_ID_SHAPE = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)

type PrivateKey = ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey


class KalshiTradeError(RuntimeError):
    """A request that failed after its tries or answered an unexpected status, or a key that
    can't sign. Its message names the call and status only: run logs are public. status is the
    unexpected status Kalshi answered, when it answered one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def clock() -> datetime:
    return datetime.now(UTC)


def load_key(pem: str, name: str = "the private key") -> PrivateKey:
    """An Ed25519 or RSA private key from its PEM text. A secret pasted as one line, with its
    newlines written as \\n, is read the same, and so is the key's base64 text pasted without its
    BEGIN and END lines (#300). A key that can't be read is described by its shape only."""
    text = pem.strip().replace("\\n", "\n")
    try:
        if "-----BEGIN" in text:
            key = serialization.load_pem_private_key(text.encode(), password=None)
        else:
            der = base64.b64decode("".join(text.split()), validate=True)
            key = serialization.load_der_private_key(der, password=None)
    except (TypeError, ValueError, UnsupportedAlgorithm):
        raise KalshiTradeError(
            f"{name} is no unencrypted PEM private key: {key_shape(text)}"
        ) from None
    if not isinstance(key, ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey):
        raise KalshiTradeError(f"{name} is neither an Ed25519 nor an RSA key")
    return key


def key_shape(text: str) -> str:
    """Why a key's text isn't a private key, from its shape and never its content (run logs are
    public): another key or the key id pasted in its place, an encrypted or cut-off key, or its
    length and lines."""
    labels = PEM_LABEL.findall(text)
    if any("PUBLIC" in label for label in labels):
        return "it is a public key, and the private key goes here"
    if any("OPENSSH" in label for label in labels):
        return "it is in OpenSSH's format, and Kalshi's key is PEM"
    if any("ENCRYPTED" in label for label in labels) or "ENCRYPTED" in text:
        return "it is encrypted"
    if KEY_ID_SHAPE.fullmatch(text):
        return "it is shaped as a key id, and the private key goes here"
    if labels and "-----END" not in text:
        return "it has its BEGIN line but no END line, as if cut off"
    begins = f"a BEGIN {labels[0]} line" if labels else "no BEGIN line"
    return f"it doesn't parse ({len(text)} characters on {len(text.splitlines())} lines, {begins})"


def sign(key: PrivateKey, text: str) -> str:
    """The base64 signature of text: Ed25519 signs it as is, RSA with RSA-PSS over SHA-256."""
    message = text.encode()
    if isinstance(key, ed25519.Ed25519PrivateKey):
        signature = key.sign(message)
    else:
        signature = key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return base64.b64encode(signature).decode()


@dataclass(frozen=True)
class Credentials:
    """An environment's API key: its id and private key, never shown in a repr."""

    key_id: str = field(repr=False)
    key: PrivateKey = field(repr=False)

    @classmethod
    def from_env(cls, env: str) -> "Credentials":
        id_name, key_name = KEY_VARIABLES[env]
        return cls(require(id_name), load_key(require(key_name), key_name))

    def headers(self, method: str, path: str, at: datetime) -> dict[str, str]:
        """The signed headers of a request at a time: path is the URL's path, without a query."""
        stamp = str(int(at.timestamp() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": stamp,
            "KALSHI-ACCESS-SIGNATURE": sign(self.key, stamp + method + path),
        }


def on_grid(value: Decimal, step: Decimal) -> bool:
    return value == value.quantize(step) and value % step == 0


@dataclass(frozen=True)
class OrderRequest:
    """A limit buy of Yes (a bid) of count contracts at most price dollars each, good until
    cancelled or until expiration_time. It fills at once from asks at or below its price and the
    rest rests, unless post_only, which refuses an order that would fill at once. Immediate or
    cancel (IOC) fills what it can at once and cancels the rest, so it takes no expiration time
    (Create Order V2)."""

    ticker: str
    client_order_id: str
    count: Decimal
    price: Decimal
    expiration_time: datetime | None = None
    post_only: bool = False
    cancel_order_on_pause: bool = True
    time_in_force: str = GTC

    def __post_init__(self) -> None:
        if not self.ticker or not self.client_order_id:
            raise ValueError("an order needs its ticker and client order id")
        if self.count < COUNT_STEP or not on_grid(self.count, COUNT_STEP):
            raise ValueError(f"an order's count is at least 0.01, in 0.01s: {self.count}")
        if not 0 < self.price < 1 or not on_grid(self.price, PRICE_STEP):
            raise ValueError(
                f"an order's price is above $0 and below $1, in $0.0001s: {self.price}"
            )
        if self.expiration_time is not None and self.expiration_time.tzinfo is None:
            raise ValueError("an order's expiration time needs its time zone")
        if self.time_in_force not in (GTC, IOC):
            raise ValueError(f"an order is {GTC} or {IOC}: {self.time_in_force}")
        if self.time_in_force == IOC and (self.expiration_time is not None or self.post_only):
            raise ValueError("an immediate-or-cancel order takes no expiry and can't be post-only")

    def body(self) -> dict[str, Any]:
        """The request body of Create Order (V2)."""
        body: dict[str, Any] = {
            "ticker": self.ticker,
            "client_order_id": self.client_order_id,
            "side": "bid",
            "count": f"{self.count:.2f}",
            "price": f"{self.price:.4f}",
            "time_in_force": self.time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": self.post_only,
            "cancel_order_on_pause": self.cancel_order_on_pause,
        }
        if self.expiration_time is not None:
            body["expiration_time"] = int(self.expiration_time.timestamp())
        return body


@dataclass(frozen=True)
class Placed:
    """A created order: its ids, whether an earlier try or run had created it (a 409), and the
    response it came from (Create Order's, or the order read back)."""

    order_id: str
    client_order_id: str
    existed: bool
    response: Mapping[str, Any]


class KalshiTrade:
    """Signed requests to one environment of Kalshi's trading API, each response stored raw before
    it is returned."""

    def __init__(
        self,
        env: str,
        credentials: Credentials,
        store: RawStore,
        client: httpx.Client | None = None,
        *,
        now: Callable[[], datetime] = clock,
        min_interval_s: float = MIN_INTERVAL_S,
        backoff_s: tuple[float, ...] = BACKOFF_S,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if env not in ENVS:
            raise ValueError(f"the environment is one of {ENVS}, not {env!r}")
        self.env = env
        self.credentials = credentials
        self.store = store
        self.client = client or httpx.Client(
            base_url=HOSTS[env] + API_ROOT, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S
        )
        self.now = now
        self.min_interval_s = min_interval_s
        self.backoff_s = backoff_s
        self.sleep = sleep
        self.monotonic = monotonic
        self._last_request = float("-inf")
        self.requests = 0

    @classmethod
    def from_env(cls, env: str, store: RawStore) -> "KalshiTrade":
        if env not in ENVS:
            raise ValueError(f"the environment is one of {ENVS}, not {env!r}")
        return cls(env, Credentials.from_env(env), store)

    def _throttle(self) -> None:
        wait = self._last_request + self.min_interval_s - self.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._last_request = self.monotonic()

    def _request(
        self,
        call: str,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        expected: Sequence[int] = (OK,),
        passed: Sequence[int] = (),
    ) -> httpx.Response:
        """One call, signed afresh at each try and stored raw: a response with an expected or a
        passed status is returned, a retryable one tried again, and any other is an error."""
        error = ""
        for attempt, backoff in enumerate((*self.backoff_s, None), start=1):
            self._throttle()
            request = self.client.build_request(method, path, params=params, json=body)
            request.headers.update(self.credentials.headers(method, request.url.path, self.now()))
            self.requests += 1
            retry_after: float | None = None
            try:
                response = self.client.send(request)
            except httpx.TransportError as exc:
                error = type(exc).__name__
            except httpx.HTTPError as exc:
                raise KalshiTradeError(
                    f"{call}: the request failed ({type(exc).__name__})"
                ) from None
            else:
                self._store(call, request, body, response, attempt)
                if response.status_code in (*expected, *passed):
                    return response
                if not retryable(response.status_code):
                    raise KalshiTradeError(
                        f"{call}: status {response.status_code}", response.status_code
                    )
                error = f"status {response.status_code}"
                retry_after = retry_after_s(response.headers.get("Retry-After"), self.now())
            if backoff is None:
                break
            self.sleep(min(max(backoff, retry_after or 0.0), MAX_WAIT_S))
        raise KalshiTradeError(f"{call}: failed after {len(self.backoff_s) + 1} tries ({error})")

    def _store(
        self,
        call: str,
        request: httpx.Request,
        body: Mapping[str, Any] | None,
        response: httpx.Response,
        attempt: int,
    ) -> str:
        fetched = self.now()
        meta = {
            "env": self.env,
            "call": call,
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.url.params),
            "sent": dict(body) if body is not None else None,
            "status": response.status_code,
            "fetched_utc": fetched.isoformat(),
            "attempt": attempt,
        }
        # The client's request count keeps two calls in the same microsecond apart.
        name = f"{fetched:%Y%m%dT%H%M%S%fZ}_{self.requests:04d}_{call}_{attempt}"
        key = f"{TRADE}/{self.env}/{fetched:%Y-%m-%d}/{name}"
        return self.store.put(SOURCE, key, response.content, meta)

    def _json(self, call: str, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        data = self._request(call, method, path, **kwargs).json()
        if not isinstance(data, dict):
            raise KalshiTradeError(f"{call}: the response is no object")
        return data

    def _pages(self, call: str, path: str, key: str, params: Mapping[str, Any]) -> list[dict]:
        """Every item of a listing, page by page, following its cursor."""
        items: list[dict] = []
        cursor = ""
        for _ in range(MAX_PAGES):
            query = {**params, "limit": PAGE} | ({"cursor": cursor} if cursor else {})
            data = self._json(call, "GET", path, params=query)
            items += data.get(key) or []
            cursor = data.get("cursor") or ""
            if not cursor:
                return items
        raise KalshiTradeError(f"{call}: more than {MAX_PAGES} pages")

    def balance(self) -> dict[str, Any]:
        """The account's balance and portfolio value (GET /portfolio/balance)."""
        return self._json("balance", "GET", "/portfolio/balance")

    def market(self, ticker: str) -> dict[str, Any]:
        """One market: its status, close time, price grid and prices (GET /markets/{ticker})."""
        return self._json("market", "GET", f"/markets/{ticker}")["market"]

    def markets(self, series: str = SERIES, status: str = "open") -> list[dict[str, Any]]:
        """A series' markets with a status (GET /markets)."""
        params = {"series_ticker": series, "status": status}
        return self._pages("markets", "/markets", "markets", params)

    def create_order(self, order: OrderRequest) -> Placed:
        """Create an order (POST /portfolio/events/orders). A 409 means an order with its client
        order id exists already, from an earlier try or run: that order is read back instead of
        sending another."""
        response = self._request(
            "create-order",
            "POST",
            "/portfolio/events/orders",
            body=order.body(),
            expected=(CREATED,),
            passed=(CONFLICT,),
        )
        if response.status_code == CONFLICT:
            found = self.find_order(order.ticker, order.client_order_id)
            if found is None:
                raise KalshiTradeError("create-order: status 409, and no order has its id")
            return Placed(found["order_id"], order.client_order_id, True, found)
        data = response.json()
        if not isinstance(data, dict) or not data.get("order_id"):
            raise KalshiTradeError("create-order: status 201 without an order id")
        return Placed(data["order_id"], order.client_order_id, False, data)

    def order(self, order_id: str) -> dict[str, Any]:
        """One order: its status, fill count, fill costs and fees (GET /portfolio/orders/{id})."""
        return self._json("order", "GET", f"/portfolio/orders/{order_id}")["order"]

    def read_order(
        self,
        order_id: str,
        until: Callable[[Mapping[str, Any]], bool],
        tries: int = READ_TRIES,
        wait_s: float = READ_WAIT_S,
    ) -> dict[str, Any] | None:
        """An order read until it shows a state, at most tries reads wait_s apart: Kalshi's reads
        lag its writes a little (Get User Data Timestamp), so a new order can be missing (404) or
        a cancel not shown yet. Returns the first read in that state, else the last read, or None
        when the order never appeared."""
        last: dict[str, Any] | None = None
        for attempt in range(tries):
            if attempt:
                self.sleep(wait_s)
            response = self._request(
                "order", "GET", f"/portfolio/orders/{order_id}", passed=(NOT_FOUND,)
            )
            if response.status_code == NOT_FOUND:
                continue
            found: dict[str, Any] = response.json()["order"]
            if until(found):
                return found
            last = found
        return last

    def orders(self, ticker: str) -> list[dict[str, Any]]:
        """The account's orders on a market, resting, cancelled or executed."""
        return self._pages("orders", "/portfolio/orders", "orders", {"ticker": ticker})

    def find_order(self, ticker: str, client_order_id: str) -> dict[str, Any] | None:
        """The order on a market with a client order id, or None."""
        found = [o for o in self.orders(ticker) if o.get("client_order_id") == client_order_id]
        if len(found) > 1:
            raise KalshiTradeError("find-order: more than one order has the client order id")
        return found[0] if found else None

    def fills(self, order_id: str) -> list[dict[str, Any]]:
        """An order's fills, each with its count, price, fee, time and whether it took."""
        return self._pages("fills", "/portfolio/fills", "fills", {"order_id": order_id})

    def cancel(self, order_id: str, ticker: str) -> dict[str, Any] | None:
        """Cancel an order's resting part (DELETE /portfolio/events/orders/{id}), or None when it
        is no longer resting (404)."""
        response = self._request(
            "cancel-order",
            "DELETE",
            f"/portfolio/events/orders/{order_id}",
            params={"market_ticker": ticker},
            passed=(NOT_FOUND,),
        )
        return None if response.status_code == NOT_FOUND else response.json()

    def amend(self, order_id: str, ticker: str, price: Decimal, count: Decimal) -> dict[str, Any]:
        """Lower a resting bid (POST /portfolio/events/orders/{id}/amend, Amend Order V2): price
        is the new limit in dollars and count the order's filled contracts plus those left to
        rest. A lower bid moves away from the asks, so it never crosses the book; the expiry is
        kept. Returns Kalshi's answer, with the order's id."""
        if not 0 < price < 1 or not on_grid(price, PRICE_STEP):
            raise ValueError(f"a price is above $0 and below $1, in $0.0001s: {price}")
        if count < COUNT_STEP or not on_grid(count, COUNT_STEP):
            raise ValueError(f"a count is at least 0.01, in 0.01s: {count}")
        body = {"ticker": ticker, "side": "bid", "price": f"{price:.4f}", "count": f"{count:.2f}"}
        found = self._json(
            "amend-order", "POST", f"/portfolio/events/orders/{order_id}/amend", body=body
        )
        if not found.get("order_id"):
            raise KalshiTradeError("amend-order: status 200 without an order id")
        return found

    def settlements(self, ticker: str) -> list[dict[str, Any]]:
        """The account's settlements on a market: its result and the payout."""
        return self._pages(
            "settlements", "/portfolio/settlements", "settlements", {"ticker": ticker}
        )


# The check (nhl kalshi check): a post-only bid of 1.50 contracts at 1¢, a fractional count as real
# orders have (#281), placed only on a market whose Yes ask is at least CHECK_FLOOR, so it can't
# fill, expiring by itself after CHECK_LIFE should the cancel fail.
CHECK_PRICE = Decimal("0.01")
CHECK_COUNT = Decimal("1.50")
CHECK_FLOOR = Decimal("0.10")
CHECK_LIFE = timedelta(minutes=2)


@dataclass
class CheckReport:
    balance_read: bool = False
    placed: bool = False
    resting: bool = False
    cancelled: bool = False
    filled: Decimal = Decimal(0)

    @property
    def passed(self) -> bool:
        """Every step held, and nothing filled: a fill, even a part of one, fails the check
        (Codex on #288)."""
        steps = self.balance_read and self.placed and self.resting and self.cancelled
        return steps and self.filled == 0


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else Decimal(0)


def check_market(listed: Sequence[Mapping[str, Any]]) -> str | None:
    """The open market a check order goes to: the one with the highest Yes ask, at least
    CHECK_FLOOR, so a 1¢ bid sits far below it. None when no market qualifies."""
    asks = [(_decimal(m.get("yes_ask_dollars")), m["ticker"]) for m in listed if m.get("ticker")]
    eligible = [pair for pair in asks if pair[0] >= CHECK_FLOOR]
    return max(eligible)[1] if eligible else None


def check(trade: KalshiTrade, echo: Callable[[str], None]) -> CheckReport:
    """Prove a key and the order path: read the balance, place a post-only bid of 1.50 contracts at
    1¢ on an open NHL market, read it back until it rests, cancel it and read it again until the
    cancel shows. Each read waits a little for the write to show (Codex on #288). Echoes the steps
    only, never a price, a market, an id or the balance."""
    report = CheckReport()
    trade.balance()
    report.balance_read = True
    echo(f"kalshi check {trade.env}: the balance was read")
    ticker = check_market(trade.markets())
    if ticker is None:
        echo(f"kalshi check {trade.env}: no open NHL market has a Yes ask of 10¢ or more")
        return report
    now = trade.now()
    order = OrderRequest(
        ticker=ticker,
        client_order_id=str(uuid.uuid4()),
        count=CHECK_COUNT,
        price=CHECK_PRICE,
        expiration_time=now + CHECK_LIFE,
        post_only=True,
    )
    placed = trade.create_order(order)
    report.placed = True
    echo(f"kalshi check {trade.env}: a bid of 1.50 contracts at 1¢ was placed")
    try:
        before = trade.read_order(placed.order_id, lambda o: o.get("status") == "resting")
        report.resting = before is not None and before.get("status") == "resting"
        echo(
            f"kalshi check {trade.env}: read back, {'resting' if report.resting else 'not resting'}"
        )
    finally:
        trade.cancel(placed.order_id, ticker)
    after = trade.read_order(placed.order_id, lambda o: o.get("status") == "canceled")
    report.cancelled = after is not None and after.get("status") == "canceled"
    report.filled = _decimal(after.get("fill_count_fp")) if after is not None else Decimal(0)
    echo(
        f"kalshi check {trade.env}: {'cancelled' if report.cancelled else 'not cancelled'}, "
        f"{report.filled:.2f} contracts filled"
    )
    return report
