"""#319's point-in-time rules for the Kalshi watch: no market is asked for at or after its game's
start, each response is dated by its own fetch, a trade is known from its fetch and never from its
own time, nothing fetched or traded at or after the start is kept, and no model or feature code
reads the watch's tables."""

import json
from datetime import timedelta
from pathlib import Path

import kalshi_fixtures as kf
import predict_fixtures as pf
from kalshi_watch_fixtures import START, TRADE_AT, Fake, run, top, trade

from nhl_edge.ingest import kalshi
from nhl_edge.ingest import kalshi_watch as kw
from nhl_edge.lake.raw import RawStore


def tickers_of(request: object) -> list[str]:
    params = request.url.params  # type: ignore[attr-defined]
    if "tickers" in params:
        return params["tickers"].split(",")
    if "ticker" in params:
        return [params["ticker"]]
    return [request.url.path.split("/")[-2]]  # type: ignore[attr-defined]


def test_no_market_is_asked_for_at_or_after_its_start(tmp_path: Path) -> None:
    # From 2 minutes before the first two games until the last one's start.
    _, served, _, _ = run(tmp_path, 600, start=pf.START - timedelta(minutes=2))
    starts = {m.ticker: m.start_utc for m in kw.markets_of(pf.slate())}
    assert served.requests
    for at, request in served.requests:
        assert all(at < starts[t] for t in tickers_of(request))


def test_each_row_is_dated_by_its_own_fetch_never_the_polls_start(tmp_path: Path) -> None:
    # Requests a second apart: each row's time is when its own request was answered, so a poll's
    # later markets are dated later than its first.
    clock = kf.Clock(START)
    served = Fake(clock)
    store = RawStore(tmp_path / "raw")
    client = kf.make(store, served, clock, min_interval_s=1.0)
    kw.watch(client=client, slate=pf.slate, until=START + timedelta(seconds=20), echo=print)
    tables, _ = kw.stored_watch(store)
    for kind in (kw.BOOKS, kw.TRADES):
        asked = {tickers_of(r)[0]: at for at, r in served.of(kind)}
        rows = tables[kind].select("ticker", "observed_utc").unique()
        assert rows.height == 6
        for ticker, observed in rows.iter_rows():
            assert observed == asked[ticker]
        assert rows["observed_utc"].n_unique() == 6


def test_a_trade_is_known_from_its_fetch_not_its_own_time(tmp_path: Path) -> None:
    _, _, store, _ = run(tmp_path, 1)
    trades = kw.stored_watch(store)[0][kw.TRADES]
    assert trades.height and (trades["created_utc"] == TRADE_AT).all()
    assert (trades["observed_utc"] > trades["created_utc"]).all()
    assert (trades["observed_utc"] >= START).all()


def test_nothing_fetched_or_traded_at_or_after_the_start_is_kept() -> None:
    market = kf.MARKET
    tops = json.dumps({"markets": [top(market.ticker)]}).encode()
    second = timedelta(seconds=1)
    trades = json.dumps(
        {
            "trades": [
                trade(market.ticker, 1, market.start_utc - second),
                trade(market.ticker, 2, market.start_utc),
            ]
        }
    ).encode()
    for at in (market.start_utc, market.start_utc + second):
        assert kw.parse_tops(tops, {market.ticker: market}, at, "k").is_empty()
        assert kw.parse_trades(trades, market, at, "k").is_empty()
        assert kalshi.parse_orderbook(kf.BOOK, market, at, "k").is_empty()
    before = market.start_utc - second
    assert kw.parse_tops(tops, {market.ticker: market}, before, "k").height == 1
    kept = kw.parse_trades(trades, market, before, "k")
    assert kept["trade_id"].to_list() == [f"{market.ticker}-1"]


def test_no_model_or_feature_code_reads_the_watch() -> None:
    # Only the watch itself, its tables' registration and schemas and the CLI that runs it name
    # them (#319): Kalshi's data is for the owner's own orders, never a model input.
    src = Path(__file__).parents[2] / "src" / "nhl_edge"
    names = ("kalshi_tops", "kalshi_depth", "kalshi_trades", "KalshiTops", "KalshiDepth")
    naming = {
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if any(name in path.read_text() for name in (*names, "KalshiTrades"))
    }
    assert naming == {"cli.py", "ingest/kalshi_watch.py", "lake/schemas.py", "lake/tables.py"}
