"""Kalshi bets in the real record (#287, ADR 0040): settlement on the exchange's rules, the nightly
check against Kalshi's own settlements, and the dashboard's copies. On the synthetic day and the
fake exchange: no test calls Kalshi or Supabase."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import predict_fixtures as pf
import pytest
import real_fixtures as rf
from fakes import ConditionalBucket
from kalshi_exchange import Exchange, trade
from typer.testing import CliRunner

from nhl_edge.cli import app
from nhl_edge.lake.raw import RawStore
from nhl_edge.live import bundle as lb
from nhl_edge.live import kalshi_trade as kt
from nhl_edge.live import placement as pm
from nhl_edge.live import real
from nhl_edge.live import real_ledger as rl

FIRST, SECOND, THIRD = 2026020053, 2026020054, 2026020055
KALSHI = real.Venue("kalshi", 0.0, 0.01, real.Exchange("live", 1.16))
REAL_DAY = rl.Mode(rl.REAL, rf.ALERT.date())
STARTS = {game_id: game[2] for game_id, game in pf.GAMES.items()}
WSH = "KXNHLGAME-26OCT07PITWSH-WSH"
PLACED_AT = rf.ALERT + timedelta(minutes=3)
NIGHT = datetime(2026, 10, 8, 12, tzinfo=UTC)


def record_on(venue: real.Venue, rows_mode: rl.Mode = REAL_DAY) -> pl.DataFrame:
    """The day's three bets placed on a venue."""
    rows = rf.alert_rows(rows_mode, venue=venue)
    entries = [rf.entry(FIRST), rf.entry(SECOND, price=1.9, stake=1.0), rf.entry(THIRD, price=2.5)]
    return rl.bets(rows, entries)


# Settlement on Kalshi's rules


def test_a_postponed_kalshi_game_settles_on_its_result_where_a_sportsbook_voids_it() -> None:
    games = rf.games(played={THIRD: pf.GAMES[THIRD][2] + timedelta(days=3)})
    book = real.Venue("book", 0.0, 0.01)
    on_kalshi = rl.results(record_on(KALSHI), games)
    at_book = rl.results(record_on(book), games)
    third = on_kalshi.filter(pl.col("game_id") == THIRD).row(0, named=True)
    assert third["status"] == rl.SETTLED and third["won"] is True
    assert third["profit"] == pytest.approx(1.5 * 1.5)
    assert at_book.filter(pl.col("game_id") == THIRD)["status"].item() == rl.VOID


def test_a_kalshi_game_played_over_two_weeks_late_is_held_for_kalshis_resolution() -> None:
    games = rf.games(played={THIRD: pf.GAMES[THIRD][2] + timedelta(days=15)})
    record = record_on(KALSHI)
    assert THIRD not in rl.results(record, games)["game_id"].to_list()
    assert rl.held(record, games)["game_id"].to_list() == [THIRD]
    # it is never settled, so the bankroll leaves it out until Kalshi resolves it
    settled = rl.settle(record, games, rl.settlements(rf.alerted_store()), NIGHT, rf.CODE)
    assert sorted(settled["game_id"].to_list()) == [FIRST, SECOND]
    lines = rl.statement(record, settled, rl.REAL, games)
    assert any("awaiting Kalshi's resolution" in line for line in lines)
    assert not any("awaiting Kalshi" in line for line in rl.statement(record, settled, rl.REAL))


# The dashboard's copies


def test_an_exchange_days_alert_rows_carry_their_order_to_the_dashboard() -> None:
    rows = rf.alert_rows(REAL_DAY, venue=KALSHI)
    sent = rl.supabase_alerts(rows)
    order = json.loads(rows["exchange"][0])
    assert sent[0]["exchange"] == order and set(sent[0]["exchange"]) >= {"limit", "contracts"}
    # a practice day sends no exchange column, so it needs no migration
    assert all("exchange" not in row for row in rl.supabase_alerts(rf.alert_rows()))


def test_a_skipped_bet_on_an_exchange_day_sends_a_null_order() -> None:
    rows = rf.alert_rows(REAL_DAY, venue=KALSHI).with_columns(
        status=pl.when(pl.col("game_id") == SECOND)
        .then(pl.lit(rl.SKIP_STARTED))
        .otherwise(pl.col("status")),
        exchange=pl.when(pl.col("game_id") == SECOND).then(None).otherwise(pl.col("exchange")),
    )
    sent = {row["game_id"]: row for row in rl.supabase_alerts(rows)}
    assert sent[SECOND]["exchange"] is None and sent[FIRST]["exchange"] is not None


class Clock:
    def __init__(self, at: datetime = PLACED_AT) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def finished(
    tmp_path: Path, fill: tuple[str, str] | None = ("1.00", "0.4700")
) -> tuple[lb.R2Store, Exchange, kt.KalshiTrade, Clock]:
    """The day's live orders placed, WSH's filled as given, all expired and reconciled, and the
    record settled on the synthetic results (WSH won at home)."""
    record, exchange, clock = lb.R2Store(ConditionalBucket(), "bucket"), Exchange(), Clock()
    client, _ = trade(tmp_path, exchange, env=pm.LIVE, now=clock)
    rows = rf.alert_rows(REAL_DAY, venue=KALSHI)
    record.put(rl.alerts_key(pf.DAY), json.dumps({"rows": rl.jsonable(rows)}).encode())
    pm.place(record, client, pm.plans(rows, pm.LIVE), clock, rf.CODE, STARTS)
    if fill is not None:
        exchange.fill("order-1", fill[0], fill[1], "0.017500", PLACED_AT)
    for order_id in exchange.orders:
        exchange.expire(order_id)
    clock.at = pf.START + timedelta(hours=4)
    pm.reconcile(record, client, clock, STARTS)
    bets = rl.bets(rl.alerts(record), rl.entries(record))
    rl.write_settlements(
        record, rl.settle(bets, rf.games(), rl.settlements(record), NIGHT, rf.CODE)
    )
    return record, exchange, client, clock


def test_the_automatic_entries_go_to_the_dashboard_marked_kalshi(tmp_path: Path) -> None:
    record, *_ = finished(tmp_path)
    automatic = {row["game_id"]: row for row in pm.automatic_entries(record)}
    assert set(automatic) == {FIRST, SECOND, THIRD}
    assert {row["source"] for row in automatic.values()} == {"kalshi"}
    assert set(automatic[FIRST]) == {*rl.ENTRY_FIELDS, "source"}
    assert (
        automatic[FIRST]["outcome"] == rl.PLACED and automatic[SECOND]["outcome"] == rl.NOT_PLACED
    )


# The nightly check against Kalshi's settlements


def kalshi_settled(result: str, count: str = "1.00", revenue: int = 100) -> list[dict[str, Any]]:
    return [
        {
            "ticker": WSH,
            "market_result": result,
            "yes_count_fp": count,
            "revenue": revenue,
            "value": None if result != "scalar" else 37,
            "settled_time": "2026-10-08T05:00:00Z",
        }
    ]


def test_the_check_agrees_when_kalshi_paid_what_the_record_settled(tmp_path: Path) -> None:
    record, exchange, client, _ = finished(tmp_path)
    exchange.settled[WSH] = kalshi_settled("yes")
    report = pm.check_settlements(record, client, rl.settlements(record), date(2026, 10, 9))
    assert (report.checked, report.agreed, report.differences) == (1, 1, [])
    assert pm.check_message(report) is None


@pytest.mark.parametrize(
    ("theirs", "found"),
    [
        (kalshi_settled("no", revenue=0), "Kalshi: lost; the record: settled, won True"),
        (kalshi_settled("scalar", revenue=37), "Kalshi resolved it as scalar, 37¢ a contract"),
        (kalshi_settled("yes", count="2.00", revenue=200), "Kalshi held 2.00 contracts"),
        (kalshi_settled("yes", revenue=90), "Kalshi paid 90¢, 100¢ expected"),
    ],
)
def test_the_check_reports_each_difference(
    tmp_path: Path, theirs: list[dict[str, Any]], found: str
) -> None:
    record, exchange, client, _ = finished(tmp_path)
    exchange.settled[WSH] = theirs
    report = pm.check_settlements(record, client, rl.settlements(record), date(2026, 10, 9))
    assert report.agreed == 0 and len(report.differences) == 1
    assert report.differences[0].startswith("2026-10-07 WSH: ") and found in report.differences[0]
    message = pm.check_message(report)
    assert message is not None and found in message


def test_a_settlement_missing_waits_two_days_then_is_a_difference(tmp_path: Path) -> None:
    record, _, client, _ = finished(tmp_path)
    soon = pm.check_settlements(record, client, rl.settlements(record), date(2026, 10, 8))
    assert soon.waiting == 1 and not soon.differences
    late = pm.check_settlements(record, client, rl.settlements(record), date(2026, 10, 9))
    assert late.differences == ["2026-10-07 WSH: not settled by Kalshi yet"]


def test_orders_without_fills_or_long_past_are_not_checked(tmp_path: Path) -> None:
    record, _, client, _ = finished(tmp_path, fill=None)
    assert (
        pm.check_settlements(record, client, rl.settlements(record), date(2026, 10, 9)).checked == 0
    )
    record, _, client, _ = finished(tmp_path / "old")
    old = date(2026, 10, 7) + timedelta(days=pm.CHECK_DAYS + 1)
    assert pm.check_settlements(record, client, rl.settlements(record), old).checked == 0


runner = CliRunner()


def local_raw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No R2 here, as on CI: the trade's raw responses go to a local store."""
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        RawStore,
        "from_env",
        classmethod(lambda cls, *args, **kwargs: RawStore(tmp_path / "cli-raw")),
    )


def test_nhl_real_reconcile_kalshi_without_the_live_key_checks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge import cli
    from nhl_edge.settings import MissingSettingError

    local_raw(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli, "_real_record", lambda r2: (None, lb.R2Store(ConditionalBucket(), "b"))
    )

    def missing(cls: Any, env: str, raw: Any) -> Any:
        raise MissingSettingError("KALSHI_KEY_ID is not set.")

    monkeypatch.setattr(kt.KalshiTrade, "from_env", classmethod(missing))
    result = runner.invoke(app, ["real", "reconcile-kalshi", "--r2"])
    assert result.exit_code == 0, result.output
    assert "no live key set, nothing to check" in result.output


def test_nhl_real_reconcile_kalshi_sends_the_differences_and_logs_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge import cli
    from nhl_edge.live import telegram

    record, exchange, client, _ = finished(tmp_path)
    exchange.settled[WSH] = kalshi_settled("no", revenue=0)
    sent: list[str] = []
    local_raw(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_real_record", lambda r2: (None, record))
    monkeypatch.setattr(kt.KalshiTrade, "from_env", classmethod(lambda cls, env, raw: client))
    monkeypatch.setattr(telegram, "send", lambda text: sent.append(text))

    class Day(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return datetime(2026, 10, 9, 12, tzinfo=UTC)

    monkeypatch.setattr(cli, "datetime", Day)
    result = runner.invoke(app, ["real", "reconcile-kalshi", "--r2"])
    assert result.exit_code == 0, result.output
    assert "kalshi settlements: 1 checked, 0 agree, 0 waiting, 1 differences" in result.output
    assert "WSH" not in result.output and len(sent) == 1 and "WSH" in sent[0]


# The entries' round trip through the dashboard


def test_the_sync_never_copies_an_automatic_entry_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from nhl_edge import cli
    from nhl_edge.lake import supabase as sb

    form = rf.entry(FIRST) | {"id": 1, "source": "form"}
    automatic = rf.entry(SECOND, price=1.9, stake=1.0) | {"id": 2, "source": "kalshi"}
    asked: list[tuple[str, ...]] = []

    class Fake:
        def select(self, table: str, columns: tuple[str, ...], order: str) -> list[dict]:
            asked.append(columns)
            return [form, automatic]

    monkeypatch.setattr(sb.Supabase, "from_env", classmethod(lambda cls: Fake()))
    store = rf.alerted_store()
    assert cli._new_entries(store) == 1
    assert [e["game_id"] for e in rl.entries(store)] == [FIRST]
    assert "source" in asked[0]


def test_before_the_kalshi_migration_the_sync_reads_entries_without_a_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nhl_edge import cli
    from nhl_edge.lake import supabase as sb

    class Fake:
        def select(self, table: str, columns: tuple[str, ...], order: str) -> list[dict]:
            if "source" in columns:
                raise sb.SupabaseError('read returned 400: {"code":"42703"}')
            return [rf.entry(FIRST) | {"id": 1}]

    monkeypatch.setattr(sb.Supabase, "from_env", classmethod(lambda cls: Fake()))
    store = rf.alerted_store()
    assert cli._new_entries(store) == 1
