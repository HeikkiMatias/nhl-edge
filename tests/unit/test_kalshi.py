"""Kalshi's order book of the alerted bets' markets (#282), on synthetic books only: the public
repository holds no Kalshi response (Developer Agreement 3.1), and no test calls Kalshi."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import kalshi_fixtures as kf
import pandera.errors
import polars as pl
import pytest
import real_fixtures as rf

from nhl_edge.ingest import kalshi
from nhl_edge.ingest.kalshi import (
    KalshiError,
    Market,
    Started,
    levels,
    markets,
    parse_orderbook,
    replay_books,
    run_books,
    slot_of,
    stored_books,
    ticker,
)
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import KalshiBooks
from nhl_edge.lake.tables import Lake
from nhl_edge.live import real_ledger as rl

PIT_WSH = "KXNHLGAME-26OCT07PITWSH"
COL_WPG = "KXNHLGAME-26OCT07COLWPG"
EDM_ANA = "KXNHLGAME-26OCT07EDMANA"


# Tickers


def test_tickers_name_the_et_date_the_away_team_first_and_the_team() -> None:
    assert ticker(date(2026, 10, 17), "NJD", "WSH", "WSH") == "KXNHLGAME-26OCT17NJWSH-WSH"
    assert ticker(date(2026, 10, 17), "NJD", "WSH", "NJD") == "KXNHLGAME-26OCT17NJWSH-NJ"
    assert ticker(date(2027, 1, 5), "TBL", "LAK", "TBL") == "KXNHLGAME-27JAN05TBLA-TB"
    assert ticker(date(2026, 12, 31), "SJS", "BOS", "SJS") == "KXNHLGAME-26DEC31SJBOS-SJ"


def test_the_slot_of_a_dispatched_run_comes_from_the_clock() -> None:
    assert slot_of(datetime(2026, 10, 7, 10, 0, tzinfo=UTC)) is None  # 06:00 ET
    assert slot_of(datetime(2026, 10, 7, 11, 5, tzinfo=UTC)) == "morning"
    assert slot_of(datetime(2026, 10, 7, 17, 30, tzinfo=UTC)) == "midday"  # 13:30 ET
    assert slot_of(datetime(2026, 10, 7, 23, 50, tzinfo=UTC)) == "pre8"
    assert slot_of(datetime(2026, 12, 8, 2, 50, tzinfo=UTC)) == "pre10"  # 21:50 EST


# Parser


def test_parses_each_level_of_both_sides_as_floats() -> None:
    frame = parse_orderbook(kf.BOOK, kf.MARKET, kf.FETCHED, "kalshi/orderbook/x")
    assert frame.select("side", "price", "quantity").rows() == [
        ("yes", 0.01, 500.0),
        ("yes", 0.35, 42.5),
        ("yes", 0.44, 120.0),
        ("no", 0.03, 250.0),
        ("no", 0.5, 7.25),
        ("no", 0.54, 60.0),
    ]
    assert frame.schema["price"] == pl.Float64 and frame.schema["quantity"] == pl.Float64
    row = frame.row(0, named=True)
    assert row["ticker"] == f"{PIT_WSH}-WSH" and row["team"] == "WSH"
    assert (row["season"], row["game_date"], row["game_id"]) == (20262027, kf.DAY, 2026020053)
    assert row["start_utc"] == kf.START and row["raw_key"] == "kalshi/orderbook/x"
    assert (frame["observed_utc"] == kf.FETCHED).all()


def test_the_best_bid_is_the_last_level_and_the_best_ask_one_less_the_last_no_bid() -> None:
    found = levels(kf.BOOK)
    best_bid, best_ask = found["yes"][-1][0], 1 - found["no"][-1][0]
    assert best_bid == 0.44 and best_ask == pytest.approx(0.46)


@pytest.mark.parametrize(
    "body",
    [
        kf.book([], []),
        kf.book(None, None),
        kf.book(drop=("yes_dollars", "no_dollars")),
        json.dumps({"orderbook_fp": None}).encode(),
    ],
    ids=["empty", "null", "missing", "null book"],
)
def test_an_empty_book_gives_no_rows(body: bytes) -> None:
    assert levels(body) == {"yes": [], "no": []}
    assert parse_orderbook(body, kf.MARKET, kf.FETCHED, "k").is_empty()


def test_one_side_may_be_empty_or_missing() -> None:
    body = kf.book([["0.4000", "10.00"]], drop=("no_dollars",))
    frame = parse_orderbook(body, kf.MARKET, kf.FETCHED, "k")
    assert frame.select("side", "price", "quantity").rows() == [("yes", 0.4, 10.0)]


def test_a_level_without_contracts_is_none() -> None:
    body = kf.book([["0.4000", "0.00"], ["0.4100", "3.50"]], [])
    assert parse_orderbook(body, kf.MARKET, kf.FETCHED, "k")["price"].to_list() == [0.41]


def test_a_response_without_an_order_book_is_refused() -> None:
    with pytest.raises(KalshiError, match="no orderbook_fp"):
        levels(json.dumps({"code": "not_found"}).encode())


def test_a_book_fetched_at_or_after_the_start_gives_no_rows() -> None:
    assert parse_orderbook(kf.BOOK, kf.MARKET, kf.START, "k").is_empty()
    assert parse_orderbook(kf.BOOK, kf.MARKET, kf.START + timedelta(seconds=1), "k").is_empty()


# Schema


def frame_of(**changes: object) -> pl.DataFrame:
    frame = parse_orderbook(kf.BOOK, kf.MARKET, kf.FETCHED, "k")
    return frame.with_columns(**{name: pl.lit(value) for name, value in changes.items()})


def test_the_schema_accepts_the_parsed_book() -> None:
    KalshiBooks.validate(frame_of())


@pytest.mark.parametrize(
    "changes",
    [
        {"price": 1.0},
        {"price": 0.0},
        {"quantity": 0.0},
        {"side": "maybe"},
        {"team": "Washington"},
        {"ticker": "KXNHLGAME-26OCT07PITWSH-PIT"},  # another team's market
        {"ticker": "KXNHLGAME-26OCT08PITWSH-WSH"},  # another date's game
        {"ticker": "KXNHLSPREAD-26OCT07PITWSH-WSH"},
    ],
    ids=lambda changes: next(iter(changes)),
)
def test_the_schema_refuses_a_bad_level(changes: dict[str, object]) -> None:
    frame = frame_of()
    bad = pl.concat(
        [frame.head(5), frame.tail(1).with_columns(**{k: pl.lit(v) for k, v in changes.items()})]
    )
    with pytest.raises(pandera.errors.SchemaError):
        KalshiBooks.validate(bad)


def test_the_schema_refuses_a_row_observed_at_the_start() -> None:
    frame = frame_of().with_columns(observed_utc=pl.col("start_utc"))
    with pytest.raises(pandera.errors.SchemaError):
        KalshiBooks.validate(frame)


def test_the_schema_refuses_a_level_listed_twice() -> None:
    frame = frame_of()
    with pytest.raises(pandera.errors.SchemaError):
        KalshiBooks.validate(pl.concat([frame, frame.head(1)]))


def test_the_schema_maps_kalshi_team_codes() -> None:
    market = Market(
        ticker(date(2026, 10, 17), "NJD", "WSH", "NJD"),
        20262027,
        date(2026, 10, 17),
        2026020099,
        datetime(2026, 10, 17, 23, tzinfo=UTC),
        "NJD",
    )
    assert parse_orderbook(kf.BOOK, market, kf.FETCHED, "k").height == 6


# Selection


def test_both_teams_markets_of_each_alerted_game_soonest_first() -> None:
    found = markets(rf.alert_rows(), rf.ALERT)
    assert [market.ticker for market in found] == [
        f"{PIT_WSH}-PIT",
        f"{PIT_WSH}-WSH",
        f"{COL_WPG}-COL",
        f"{COL_WPG}-WPG",
        f"{EDM_ANA}-EDM",
        f"{EDM_ANA}-ANA",
    ]
    assert [market.team for market in found[:2]] == ["PIT", "WSH"]
    assert {market.game_date for market in found} == {kf.DAY}


def test_only_alerted_bets_games_are_asked_for() -> None:
    rows = rf.alert_rows().with_columns(
        status=pl.when(pl.col("game_id") == 2026020054)
        .then(pl.lit(rl.SKIP_MINIMUM))
        .otherwise(pl.col("status"))
    )
    assert {market.game_id for market in markets(rows, rf.ALERT)} == {2026020053, 2026020055}
    paused = rf.alert_rows(rl.Mode(rl.PAUSED, rf.ALERT.date()))
    assert markets(paused, rf.ALERT) == []


def test_a_game_that_has_started_is_never_asked_for() -> None:
    start = datetime(2026, 10, 7, 23, 30, tzinfo=UTC)
    tickers = {market.ticker for market in markets(rf.alert_rows(), start)}
    assert tickers == {f"{EDM_ANA}-EDM", f"{EDM_ANA}-ANA"}
    later = datetime(2026, 10, 8, 2, tzinfo=UTC)
    assert markets(rf.alert_rows(), later) == []


# The client: throttle, backoff, 404


def test_requests_are_throttled(tmp_path: Path) -> None:
    clock = kf.Clock()
    paths, handler = kf.serve()
    api = kf.make(RawStore(tmp_path), handler, clock, min_interval_s=0.25)
    first, second = markets(rf.alert_rows(), rf.ALERT)[:2]
    api.orderbook(first, "midday")
    api.orderbook(second, "midday")
    assert len(paths) == 2 and clock.sleeps == [0.25]


def test_the_raw_copy_is_untouched_and_keyed_by_date_fetch_time_and_ticker(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    paths, handler = kf.serve()
    fetched = kf.make(store, handler).orderbook(kf.MARKET, "midday")
    assert paths == [f"/trade-api/v2/markets/{PIT_WSH}-WSH/orderbook"]
    assert fetched.raw_key == f"kalshi/orderbook/2026-10-07/20261007T165500Z_{PIT_WSH}-WSH"
    assert store.get(fetched.raw_key) == kf.BOOK
    meta = store.meta(fetched.raw_key)
    assert (
        meta["url"]
        == f"https://api.elections.kalshi.com/trade-api/v2/markets/{PIT_WSH}-WSH/orderbook"
    )
    assert meta["status"] == 200 and meta["attempts"] == 1 and meta["slot"] == "midday"
    assert meta["fetched_utc"] == kf.FETCHED.isoformat()
    assert (meta["ticker"], meta["game_id"], meta["team"]) == (kf.MARKET.ticker, 2026020053, "WSH")
    assert kalshi.market_of(meta) == kf.MARKET


def test_429_is_retried_with_exponential_backoff(tmp_path: Path) -> None:
    clock = kf.Clock()
    requests, handler = kf.sequence(
        httpx.Response(429, json={"error": "too many requests"}),
        httpx.Response(503),
        httpx.Response(200, content=kf.BOOK),
    )
    store = RawStore(tmp_path)
    fetched = kf.make(store, handler, clock).orderbook(kf.MARKET, None)
    assert len(requests) == 3 and clock.sleeps == [1.0, 2.0]
    assert store.meta(fetched.raw_key)["attempts"] == 3


def test_after_five_tries_the_market_fails_and_nothing_is_stored(tmp_path: Path) -> None:
    clock = kf.Clock()
    requests, handler = kf.sequence(*[httpx.Response(429)] * 5)
    with pytest.raises(KalshiError, match="after 5 tries: status 429") as caught:
        kf.make(RawStore(tmp_path), handler, clock).orderbook(kf.MARKET, None)
    assert len(requests) == 5 and clock.sleeps == [1.0, 2.0, 4.0, 8.0]
    assert "KXNHL" not in str(caught.value)
    assert not list(tmp_path.rglob("*.json.gz"))


def test_a_retry_after_is_kept_within_the_bound(tmp_path: Path) -> None:
    clock = kf.Clock()
    _, handler = kf.sequence(
        httpx.Response(429, headers={"Retry-After": "600"}), httpx.Response(200, content=kf.BOOK)
    )
    kf.make(RawStore(tmp_path), handler, clock).orderbook(kf.MARKET, None)
    assert clock.sleeps == [kalshi.MAX_WAIT_S]


def test_transport_errors_are_retried(tmp_path: Path) -> None:
    requests, handler = kf.sequence(
        httpx.ConnectTimeout("slow"), httpx.Response(200, content=kf.BOOK)
    )
    assert kf.make(RawStore(tmp_path), handler).orderbook(kf.MARKET, None).status == 200
    assert len(requests) == 2


def test_a_404_is_stored_as_not_found_and_never_retried(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    requests, handler = kf.sequence(httpx.Response(404, json={"code": "not_found"}))
    fetched = kf.make(store, handler).orderbook(kf.MARKET, None)
    assert len(requests) == 1 and fetched.status == 404
    assert store.meta(fetched.raw_key)["status"] == 404


def test_another_status_fails_at_once_and_stores_nothing(tmp_path: Path) -> None:
    requests, handler = kf.sequence(httpx.Response(403))
    with pytest.raises(KalshiError, match="status 403"):
        kf.make(RawStore(tmp_path), handler).orderbook(kf.MARKET, None)
    assert len(requests) == 1 and not list(tmp_path.rglob("*.json.gz"))


def test_a_game_that_starts_during_the_backoff_is_not_asked_again(tmp_path: Path) -> None:
    clock = kf.Clock(kf.START - timedelta(seconds=2))
    requests, handler = kf.sequence(httpx.Response(429), httpx.Response(429))
    with pytest.raises(Started):
        kf.make(RawStore(tmp_path), handler, clock).orderbook(kf.MARKET, None)
    # Asked 2 seconds and 1 second before the start; the third try would come after it.
    assert len(requests) == 2


# The poll and its replay


def poll(
    store: RawStore, clock: kf.Clock, missing: frozenset[str] = frozenset()
) -> tuple[kalshi.PollReport, list[str], list[str]]:
    paths, handler = kf.serve(missing=missing)
    lines: list[str] = []
    report = run_books(
        kalshi=kf.make(store, handler, clock),
        alerts=rf.alert_rows(),
        slot="midday",
        echo=lines.append,
    )
    return report, paths, lines


def test_the_poll_stores_every_alerted_market_and_prints_counts_only(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    report, paths, lines = poll(store, kf.Clock(), frozenset({f"{COL_WPG}-WPG"}))
    assert len(paths) == 6
    assert (report.markets, report.stored, report.not_found, report.failed) == (6, 5, 1, 0)
    assert len(list(tmp_path.rglob("*.json.gz"))) == 6  # the 404 is kept raw too
    # Run logs are public: no market, price or size reaches them.
    text = "\n".join(lines)
    assert "KXNHL" not in text and "0.44" not in text and "120" not in text
    assert "6 markets of 3 games, 5 books stored; 1 not found, 0 failed" in text


def test_a_failed_market_does_not_stop_the_others(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if "-PIT/" in request.url.path:
            return httpx.Response(500)
        return httpx.Response(200, content=kf.BOOK)

    lines: list[str] = []
    report = run_books(
        kalshi=kf.make(RawStore(tmp_path), handler),
        alerts=rf.alert_rows(),
        slot=None,
        echo=lines.append,
    )
    assert (report.stored, report.failed) == (5, 1) and len(calls) == 10
    assert any(line.startswith("::warning::") and "status 500" in line for line in lines)


def test_without_an_alerted_game_to_come_nothing_is_fetched(tmp_path: Path) -> None:
    clock = kf.Clock(datetime(2026, 10, 8, 2, tzinfo=UTC))
    lines: list[str] = []
    report = run_books(
        kalshi=kf.make(RawStore(tmp_path), kf.fail, clock),
        alerts=rf.alert_rows(),
        slot="pre10",
        echo=lines.append,
    )
    assert report.markets == 0 and "nothing fetched" in lines[0]


def test_replay_rebuilds_the_table_from_raw_alone(tmp_path: Path) -> None:
    store, lake = RawStore(tmp_path / "raw"), Lake(tmp_path / "lake")
    clock = kf.Clock()
    poll(store, clock, frozenset({f"{COL_WPG}-WPG"}))
    clock.advance(timedelta(hours=6))  # 22:55 UTC: the pre7 slot, before the 23:30 starts
    poll(store, clock)
    report = replay_books(store, lake, [kf.DAY])
    assert (report.responses, report.not_found, report.empty, report.rows) == (12, 1, 0, 66)
    table = lake.read("kalshi_books")
    assert table.height == 66
    assert table["observed_utc"].unique().sort().to_list() == [
        kf.FETCHED,
        kf.FETCHED + timedelta(hours=6),
    ]
    assert set(table["game_date"]) == {kf.DAY} and set(table["season"]) == {20262027}
    # A rerun rewrites the same partition, so nothing doubles.
    replay_books(store, lake, [kf.DAY])
    assert lake.read("kalshi_books").height == 66


def test_replay_counts_empty_unparsed_and_incomplete_responses(tmp_path: Path) -> None:
    store, lake = RawStore(tmp_path / "raw"), Lake(tmp_path / "lake")
    market = kf.MARKET
    meta = {
        "status": 200,
        "fetched_utc": kf.FETCHED.isoformat(),
        "ticker": market.ticker,
        "season": market.season,
        "game_date": market.game_date.isoformat(),
        "game_id": market.game_id,
        "start_utc": market.start_utc.isoformat(),
        "team": market.team,
    }
    prefix = f"orderbook/{kf.DAY.isoformat()}"
    store.put("kalshi", f"{prefix}/20261007T165500Z_{market.ticker}", kf.book([], []), meta)
    store.put("kalshi", f"{prefix}/20261007T170000Z_{market.ticker}", b"{}", meta)
    late = meta | {"fetched_utc": kf.START.isoformat()}
    store.put("kalshi", f"{prefix}/20261007T233000Z_{market.ticker}", kf.BOOK, late)
    cut = store.put("kalshi", f"{prefix}/20261007T171000Z_{market.ticker}", kf.BOOK, meta)
    (store.base_dir / f"{cut}.meta.json").unlink()
    table, report = stored_books(store, [kf.DAY])
    assert table.is_empty()
    assert (report.responses, report.empty, report.after_start) == (3, 1, 1)
    assert report.unparsed == [f"kalshi/{prefix}/20261007T170000Z_{market.ticker}"]
    assert report.incomplete == [cut]
    # An empty day deletes its partition rather than leaving stale rows.
    lake.write("kalshi_books", parse_orderbook(kf.BOOK, market, kf.FETCHED, "old"))
    replay_books(store, lake, [kf.DAY])
    assert lake.read("kalshi_books").is_empty()


def test_replay_reads_only_the_dates_asked_for(tmp_path: Path) -> None:
    store, lake = RawStore(tmp_path / "raw"), Lake(tmp_path / "lake")
    poll(store, kf.Clock())
    _, report = stored_books(store, [date(2026, 10, 8)])
    assert report.responses == 0 and report.dates == []
    assert replay_books(store, lake, None).dates == [kf.DAY]
