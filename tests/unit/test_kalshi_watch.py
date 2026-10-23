"""#319: every game's Kalshi markets watched from the morning to the start, on synthetic responses
only: the public repository holds no Kalshi response (Developer Agreement 3.1), and no test calls
Kalshi."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import kalshi_fixtures as kf
import polars as pl
import predict_fixtures as pf
import pytest
from kalshi_watch_fixtures import START, TRADE_AT, Fake, run, top, trade

from nhl_edge.ingest import kalshi
from nhl_edge.ingest import kalshi_watch as kw
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.tables import Lake

# The fixture day's first window, 07:05 EDT: a run's first trades poll reaches back to it.
MORNING = datetime(2026, 10, 7, 11, 5, tzinfo=UTC)


def test_a_run_ends_a_minute_before_the_next_window_and_never_after_its_longest() -> None:
    assert kw.window_end(datetime(2026, 10, 23, 11, 5, tzinfo=UTC)) == datetime(
        2026, 10, 23, 15, 4, tzinfo=UTC
    )  # 07:05 EDT to 11:04 EDT
    assert kw.window_end(datetime(2026, 12, 1, 16, 5, tzinfo=UTC)) == datetime(
        2026, 12, 1, 20, 4, tzinfo=UTC
    )  # 11:05 EST to 15:04 EST
    last = datetime(2026, 10, 23, 23, 5, tzinfo=UTC)  # 19:05 EDT
    assert kw.window_end(last) == last + kw.LONGEST
    late = datetime(2026, 10, 23, 13, 30, tzinfo=UTC)  # a late fallback run at 09:30 EDT
    assert kw.window_end(late) == datetime(2026, 10, 23, 15, 4, tzinfo=UTC)


def test_every_game_has_both_teams_markets_soonest_first() -> None:
    markets = kw.markets_of(pf.slate())
    assert [m.ticker for m in markets[:2]] == [
        "KXNHLGAME-26OCT07PITWSH-PIT",
        "KXNHLGAME-26OCT07PITWSH-WSH",
    ]
    assert len(markets) == 6 and markets[-1].start_utc == pf.GAMES[2026020055][2]


def test_tops_every_minute_and_books_and_trades_every_five(tmp_path: Path) -> None:
    report, served, _, _ = run(tmp_path, 12)
    assert report.polls == {kw.TOPS: 12, kw.BOOKS: 3, kw.TRADES: 3}
    assert len(served.of(kw.TOPS)) == 12
    # One request for every market's top; one book and one trades page per market and poll.
    assert len(served.of(kw.BOOKS)) == len(served.of(kw.TRADES)) == 3 * 6
    times = sorted({at for at, _ in served.of(kw.BOOKS)})
    assert times == [START + timedelta(minutes=m) for m in (0, 5, 10)]


def test_trades_reach_back_over_the_last_poll_with_an_overlap(tmp_path: Path) -> None:
    _, served, _, _ = run(tmp_path, 7)
    asked = sorted({int(r.url.params["min_ts"]) for _, r in served.of(kw.TRADES)})
    first = MORNING - kw.OVERLAP
    second = START - kw.OVERLAP
    assert asked == [int(first.timestamp()), int(second.timestamp())]


def test_trade_pages_follow_the_cursor(tmp_path: Path) -> None:
    _, served, _, _ = run(tmp_path, 1, fake=lambda clock: Fake(clock, pages=3))
    wsh = [r for _, r in served.of(kw.TRADES) if r.url.params["ticker"].endswith("-WSH")]
    assert [r.url.params.get("cursor") for r in wsh] == [None, "2", "3"]


def test_the_watch_stops_at_the_last_start_and_never_asks_after_a_games_start(
    tmp_path: Path,
) -> None:
    # 3 minutes before the first two games, and long before the third.
    early = pf.START - timedelta(minutes=3)
    report, served, _, _ = run(tmp_path, 600, start=early)
    by_game = {m.ticker: m.start_utc for m in kw.markets_of(pf.slate())}
    for at, request in served.requests:
        params = request.url.params
        if "tickers" in params:
            tickers = params["tickers"].split(",")
        elif "ticker" in params:
            tickers = [params["ticker"]]
        else:
            tickers = [request.url.path.split("/")[-2]]
        assert all(at < by_game[t] for t in tickers)
    # Only the last game's markets are asked for once the first two have started.
    later = [r for at, r in served.of(kw.TOPS) if at >= pf.START]
    assert later and all(r.url.params["tickers"].count(",") == 1 for r in later)
    assert max(at for at, _ in served.requests) < pf.GAMES[2026020055][2]
    assert report.polls[kw.TOPS] > 100


def test_a_market_not_found_is_counted_and_kept(tmp_path: Path) -> None:
    missing = frozenset({"KXNHLGAME-26OCT07PITWSH-WSH"})
    report, _, store, _ = run(tmp_path, 1, fake=lambda clock: Fake(clock, missing=missing))
    assert report.not_found == 1
    _, replayed = kw.stored_watch(store)
    assert replayed.not_found == 1


def test_the_log_has_counts_only(tmp_path: Path) -> None:
    _, _, _, lines = run(tmp_path, 6)
    assert lines == [
        "kalshi watch: 6 top, 2 book and 2 trade polls stored in 30 requests; 0 not found, "
        "0 failed, 0 skipped as started"
    ]


def test_the_replay_rebuilds_the_three_tables_and_keeps_a_trade_once(tmp_path: Path) -> None:
    _, _, store, _ = run(tmp_path, 12)
    tables, report = kw.stored_watch(store)
    assert report.objects == {kw.TOPS: 12, kw.BOOKS: 3, kw.TRADES: 3}
    assert report.unparsed == [] and report.incomplete == []
    tops = tables[kw.TOPS]
    assert tops.height == 12 * 6
    row = tops.filter(pl.col("ticker") == "KXNHLGAME-26OCT07PITWSH-WSH").row(0, named=True)
    assert (row["yes_bid"], row["yes_ask"], row["yes_ask_size"]) == (0.44, 0.46, 60.0)
    assert row["game_id"] == 2026020053 and row["team"] == "WSH"
    # Six levels in the synthetic book, for each market and poll.
    assert tables[kw.BOOKS].height == 3 * 6 * 6
    # Each poll returns the same trade again: it is kept once, at its first fetch.
    trades = tables[kw.TRADES]
    assert trades.height == 6
    assert (trades["observed_utc"] == START).all()
    assert (trades["created_utc"] == TRADE_AT).all()

    lake = Lake(tmp_path / "lake")
    kw.replay(store, lake, [pf.DAY])
    for kind in kw.KINDS:
        assert lake.read(kw.TABLES[kind]).height == report.rows[kind]


def test_a_slate_refetch_follows_a_postponed_game(tmp_path: Path) -> None:
    slates = [pf.slate(), pf.slate().filter(pl.col("game_id") != 2026020053)]
    clock = kf.Clock(START)
    served = Fake(clock)
    store = RawStore(tmp_path / "raw")
    fetched = iter(slates)
    kw.watch(
        client=kf.make(store, served, clock),
        slate=lambda: next(fetched, slates[-1]),
        until=START + timedelta(minutes=35),
        echo=print,
    )
    after = [r for at, r in served.of(kw.TOPS) if at >= START + kw.REFRESH]
    assert after and all("PITWSH" not in r.url.params["tickers"] for r in after)


def test_a_failed_request_is_counted(tmp_path: Path) -> None:
    def broken(clock: kf.Clock) -> Fake:
        class Broken(Fake):
            def __call__(self, request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/orderbook"):
                    self.requests.append((self.clock.now(), request))
                    return httpx.Response(400, json={"code": "bad_request"})
                return super().__call__(request)

        return Broken(clock)

    report, _, _, lines = run(tmp_path, 1, fake=broken)
    assert report.failed == 6 and report.polls[kw.BOOKS] == 0
    assert "6 failed" in lines[0]


def test_a_response_fetched_at_or_after_the_start_gives_no_rows() -> None:
    market = kf.MARKET
    tops = json.dumps({"markets": [top(market.ticker)]}).encode()
    trades = json.dumps({"trades": [trade(market.ticker)]}).encode()
    for at in (market.start_utc, market.start_utc + timedelta(seconds=1)):
        assert kw.parse_tops(tops, {market.ticker: market}, at, "k").is_empty()
        assert kw.parse_trades(trades, market, at, "k").is_empty()


def test_a_trade_made_at_or_after_the_start_is_never_kept() -> None:
    market = kf.MARKET
    body = json.dumps(
        {
            "trades": [
                trade(market.ticker, 1, market.start_utc - timedelta(seconds=1)),
                trade(market.ticker, 2, market.start_utc),
            ]
        }
    ).encode()
    kept = kw.parse_trades(
        body, market, market.start_utc - timedelta(minutes=1) + timedelta(seconds=59), "k"
    )
    assert kept["trade_id"].to_list() == [f"{market.ticker}-1"]


def test_a_listing_without_markets_is_refused() -> None:
    with pytest.raises(kalshi.KalshiError):
        kw.parse_tops(b'{"cursor": ""}', {}, START, "k")


def test_nhl_kalshi_watch_polls_and_builds_the_days_tables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app
    from nhl_edge.ingest import nhl_api
    from nhl_edge.live import slate as live_slate

    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("nhl_edge.settings.load_env", lambda: None)
    monkeypatch.chdir(tmp_path)
    clock = kf.Clock(START)
    served = Fake(clock)
    monkeypatch.setattr(nhl_api, "utc_now", clock.now)
    monkeypatch.setattr(kalshi, "Kalshi", lambda store: kf.make(store, served, clock))
    fetched = live_slate.Fetched(pf.slate(), "nhl/schedule/x", START)
    monkeypatch.setattr(live_slate, "fetch", lambda api, day: fetched)
    result = CliRunner().invoke(app, ["kalshi", "watch", "--until", "2026-10-07T17:01"])
    assert result.exit_code == 0, result.output
    assert "kalshi watch: 6 top, 2 book and 2 trade polls stored" in result.output
    assert "kalshi watch 2026-10-07: rebuilt from 10 stored polls" in result.output
    # Run logs are public: no market or figure from Kalshi is printed.
    assert "KXNHL" not in result.output and "0.44" not in result.output
    lake = Lake()
    assert lake.read("kalshi_tops").height == 6 * 6
    assert lake.read("kalshi_depth").height == 2 * 6 * 6
    assert lake.read("kalshi_trades").height == 6

    # nhl kalshi replay rebuilds the same rows from the raw polls alone.
    replayed = CliRunner().invoke(app, ["kalshi", "replay"])
    assert replayed.exit_code == 0, replayed.output
    assert "kalshi watch replay 2026-10-07..2026-10-07: 6 tops polls, 36 rows" in replayed.output
    assert lake.read("kalshi_tops").height == 36


def test_a_failed_trades_request_keeps_that_markets_watermark(tmp_path: Path) -> None:
    # WSH's trades fail in the first poll: its next poll reaches back to where the first began,
    # while the others reach back only over the overlap (Codex on #325).
    wsh = "KXNHLGAME-26OCT07PITWSH-WSH"

    def flaky(clock: kf.Clock) -> Fake:
        class Flaky(Fake):
            def __call__(self, request: httpx.Request) -> httpx.Response:
                first = self.clock.now() < START + timedelta(minutes=1)
                trades = request.url.path.endswith("/markets/trades")
                if first and trades and request.url.params["ticker"] == wsh:
                    self.requests.append((self.clock.now(), request))
                    return httpx.Response(400, json={"code": "bad_request"})
                return super().__call__(request)

        return Flaky(clock)

    report, served, _, _ = run(tmp_path, 7, fake=flaky)
    assert report.failed == 1
    second = [r for at, r in served.of(kw.TRADES) if at >= START + kw.BOOK_EVERY]
    marks = {r.url.params["ticker"]: int(r.url.params["min_ts"]) for r in second}
    first_start = MORNING - kw.OVERLAP
    assert marks[wsh] == int(first_start.timestamp())
    others = {t: m for t, m in marks.items() if t != wsh}
    assert set(others.values()) == {int((START - kw.OVERLAP).timestamp())}


def test_a_cut_short_page_run_keeps_the_watermark(tmp_path: Path) -> None:
    # More pages than MAX_PAGES: the market isn't done, so its watermark stays.
    pages = kw.MAX_PAGES + 1
    _, served, _, _ = run(tmp_path, 7, fake=lambda clock: Fake(clock, pages=pages))
    second = [r for at, r in served.of(kw.TRADES) if at >= START + kw.BOOK_EVERY]
    assert {int(r.url.params["min_ts"]) for r in second} == {
        int((MORNING - kw.OVERLAP).timestamp())
    }


def test_a_replay_with_an_unreadable_poll_replaces_nothing(tmp_path: Path) -> None:
    _, _, store, _ = run(tmp_path, 2)
    lake = Lake(tmp_path / "lake")
    kw.replay(store, lake, [pf.DAY])
    before = lake.read("kalshi_tops").height
    assert before == 2 * 6
    # A stored 200 whose body is no listing: the replay stops before any partition changes.
    bad = {
        "responses": [
            {
                "ticker": None,
                "status": 200,
                "fetched_utc": START.isoformat(),
                "attempts": 1,
                "body": "{}",
            }
        ]
    }
    markets = [
        {
            "ticker": m.ticker,
            "season": m.season,
            "game_date": m.game_date.isoformat(),
            "game_id": m.game_id,
            "start_utc": m.start_utc.isoformat(),
            "team": m.team,
        }
        for m in kw.markets_of(pf.slate())
    ]
    store.put(
        "kalshi",
        f"watch/tops/{pf.DAY.isoformat()}/20261007T170500Z",
        json.dumps(bad).encode(),
        {"kind": "tops", "markets": markets},
    )
    with pytest.raises(kw.ReplayError, match="1 unparsed and 0 incomplete"):
        kw.replay(store, lake, [pf.DAY])
    assert lake.read("kalshi_tops").height == before


def test_a_run_started_before_the_first_window_or_on_the_other_offsets_line_watches_nothing() -> (
    None
):
    assert not kw.watch_hours(datetime(2026, 10, 23, 10, 59, tzinfo=UTC))  # 06:59 EDT
    assert kw.watch_hours(datetime(2026, 10, 23, 11, 5, tzinfo=UTC))  # 07:05 EDT
    assert kw.watch_hours(datetime(2026, 10, 24, 3, 59, tzinfo=UTC))  # 23:59 EDT
    assert not kw.watch_hours(datetime(2026, 10, 24, 6, 5, tzinfo=UTC))  # 02:05 EDT
    october, december = (
        datetime(2026, 10, 23, 15, 5, tzinfo=UTC),
        datetime(2026, 12, 1, 16, 5, tzinfo=UTC),
    )
    edt, est = list(kw.FALLBACK_CRONS)
    assert kw.cron_due(edt, october) and not kw.cron_due(est, october)
    assert kw.cron_due(est, december) and not kw.cron_due(edt, december)
    assert not kw.cron_due("0 3 * * *", october)


def test_nhl_kalshi_watch_skips_the_other_offsets_line_and_an_early_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app
    from nhl_edge.ingest import nhl_api

    monkeypatch.setattr("nhl_edge.settings.load_env", lambda: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(kalshi, "Kalshi", lambda store: kf.make(store, kf.fail))
    est = list(kw.FALLBACK_CRONS)[1]
    clock = kf.Clock(datetime(2026, 10, 23, 15, 5, tzinfo=UTC))  # 11:05 EDT
    monkeypatch.setattr(nhl_api, "utc_now", clock.now)
    skipped = CliRunner().invoke(app, ["kalshi", "watch", "--cron", est])
    assert skipped.exit_code == 0, skipped.output
    assert "is not this time of year's line, skipped" in skipped.output
    early = kf.Clock(datetime(2026, 10, 24, 6, 5, tzinfo=UTC))  # 02:05 EDT, a late fallback
    monkeypatch.setattr(nhl_api, "utc_now", early.now)
    nothing = CliRunner().invoke(app, ["kalshi", "watch"])
    assert nothing.exit_code == 0, nothing.output
    assert "started before the first window (07:05 ET), nothing watched" in nothing.output


def test_a_runs_first_trades_poll_reaches_back_to_the_days_first_window(tmp_path: Path) -> None:
    # A 15:05 EDT run: its first poll of each market asks from 07:04 EDT, so a trades poll that
    # failed at the end of the 11:05 run leaves no gap (Codex on #325).
    afternoon = datetime(2026, 10, 7, 19, 5, tzinfo=UTC)
    _, served, _, _ = run(tmp_path, 1, start=afternoon)
    asked = {int(r.url.params["min_ts"]) for _, r in served.of(kw.TRADES)}
    assert asked == {int((MORNING - kw.OVERLAP).timestamp())}


def test_a_trades_page_without_its_list_is_kept_raw_marked_invalid_and_counted(
    tmp_path: Path,
) -> None:
    def hollow(clock: kf.Clock) -> Fake:
        class Hollow(Fake):
            def __call__(self, request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/markets/trades"):
                    self.requests.append((self.clock.now(), request))
                    return httpx.Response(200, json={"cursor": ""})
                return super().__call__(request)

        return Hollow(clock)

    report, served, store, lines = run(tmp_path, 7, fake=hollow)
    # Every market's trades failed in both polls, and none advanced its watermark.
    assert report.failed == 2 * 6 and "12 failed" in lines[0]
    second = [r for at, r in served.of(kw.TRADES) if at >= START + kw.BOOK_EVERY]
    assert {int(r.url.params["min_ts"]) for r in second} == {
        int((MORNING - kw.OVERLAP).timestamp())
    }
    # The replay skips them, as the poll marked them, and rebuilds the rest.
    tables, replayed = kw.stored_watch(store)
    assert replayed.invalid == 12 and replayed.unparsed == []
    assert tables[kw.TRADES].is_empty() and tables[kw.TOPS].height == 7 * 6
    kw.replay(store, Lake(tmp_path / "lake"), [pf.DAY])


def test_a_trades_page_without_its_list_is_refused_by_the_parser() -> None:
    with pytest.raises(kalshi.KalshiError):
        kw.parse_trades(b'{"cursor": ""}', kf.MARKET, START, "k")


def test_no_request_starts_after_the_window_ends(tmp_path: Path) -> None:
    # Requests 30 seconds apart: a book poll at the start would take 6 minutes, but nothing is
    # asked once the 2-minute window is over (Codex on #325).
    clock = kf.Clock(START)
    served = Fake(clock)
    store = RawStore(tmp_path / "raw")
    client = kf.make(store, served, clock, min_interval_s=30.0)
    until = START + timedelta(minutes=2)
    report = kw.watch(client=client, slate=pf.slate, until=until, echo=print)
    assert served.requests and max(at for at, _ in served.requests) < until
    assert report.failed == 0 and report.started == 0
