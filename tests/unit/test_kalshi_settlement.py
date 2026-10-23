"""Kalshi bets in the real record (#287, ADR 0040): settlement on the exchange's rules, the nightly
check against Kalshi's own settlements, and the dashboard's copies. On the synthetic day and the
fake exchange: no test calls Kalshi or Supabase."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandera.errors
import polars as pl
import predict_fixtures as pf
import pytest
import real_fixtures as rf
from fakes import ConditionalBucket
from kalshi_exchange import Exchange, trade
from typer.testing import CliRunner

from nhl_edge.cli import app
from nhl_edge.lake.raw import RawStore
from nhl_edge.lake.schemas import RealSettlements
from nhl_edge.live import bundle as lb
from nhl_edge.live import kalshi_trade as kt
from nhl_edge.live import placement as pm
from nhl_edge.live import real
from nhl_edge.live import real_ledger as rl

FIRST, SECOND, THIRD = 2026020053, 2026020054, 2026020055
KALSHI = real.Venue("kalshi", 0.0, 0.01, real.Exchange("live", 1.16))


@pytest.fixture(autouse=True)
def kalshi_venue(monkeypatch: pytest.MonkeyPatch) -> None:
    """The code names Kalshi's live exchange, as the venue commit will (#294)."""
    monkeypatch.setattr(real, "VENUES", {"kalshi": KALSHI})
    monkeypatch.setattr(real, "VENUE", "kalshi")


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


# Kalshi's own payout of a held bet (#290)


def resolved(
    game_id: int = THIRD,
    result: str = "scalar",
    revenue: int = 37,
    contracts: float = 1.0,
    observed: datetime = NIGHT,
) -> pl.DataFrame:
    """Kalshi's settlement of a bet's market, as rl.exchange_settlements reads it back."""
    row = {
        "game_date": pf.DAY,
        "game_id": game_id,
        "market_result": result,
        "contracts": contracts,
        "revenue": revenue,
        "observed_utc": observed,
    }
    return pl.DataFrame([row], schema=rl.RESOLVED)


LATE = {THIRD: pf.GAMES[THIRD][2] + timedelta(days=15)}


@pytest.mark.parametrize(("result", "revenue"), [("scalar", 37), ("yes", 100), ("no", 0)])
def test_a_held_kalshi_bet_is_paid_out_at_kalshis_price(result: str, revenue: int) -> None:
    games = rf.games(played=LATE)
    paid = rl.results(record_on(KALSHI), games, resolved(result=result, revenue=revenue))
    third = paid.filter(pl.col("game_id") == THIRD).row(0, named=True)
    # Never a win or a loss on the full game: Kalshi's payout per dollar the bet cost.
    assert third["status"] == rl.PAYOUT and third["won"] is None and third["home_win"] is None
    assert third["profit"] == pytest.approx(1.5 * (revenue / 100 * 2.5 - 1))
    assert third["result_utc"] == NIGHT
    assert sorted(paid["game_id"].to_list()) == [FIRST, SECOND, THIRD]


def test_a_payout_settles_once_validated_and_the_statement_names_it() -> None:
    games = rf.games(played=LATE)
    record = record_on(KALSHI)
    settled = rl.settle(
        record, games, rl.settlements(rf.alerted_store()), NIGHT, rf.CODE, resolved()
    )
    third = settled.filter(pl.col("game_id") == THIRD).row(0, named=True)
    assert third["status"] == rl.PAYOUT and third["profit"] == pytest.approx(-0.1125)
    lines = rl.statement(record, settled, rl.REAL, games)
    assert any("paid out at the exchange's own price, -0.11 €" in line for line in lines)
    assert not any("awaiting Kalshi" in line for line in lines)
    # Read back the next night, it is settled already: nothing is appended twice.
    again = rl.settle(record, games, settled, NIGHT + timedelta(days=1), rf.CODE, resolved())
    assert again.is_empty()


def test_a_game_never_played_is_paid_out_only_at_a_fair_price() -> None:
    # A cancelled game, or one still to be played: a fair price (scalar) pays it out, and a yes
    # or no waits for the lake's own result on the full game.
    unplayed = rf.games().filter(pl.col("game_id") != THIRD)
    record = record_on(KALSHI)
    fair = rl.results(record, unplayed, resolved())
    assert fair.filter(pl.col("game_id") == THIRD)["status"].to_list() == [rl.PAYOUT]
    for result, revenue in (("yes", 100), ("no", 0)):
        waits = rl.results(record, unplayed, resolved(result=result, revenue=revenue))
        assert THIRD not in waits["game_id"].to_list()


def test_a_game_played_on_time_settles_on_its_result_whatever_kalshi_paid() -> None:
    on_time = rl.results(record_on(KALSHI), rf.games(), resolved())
    third = on_time.filter(pl.col("game_id") == THIRD).row(0, named=True)
    assert third["status"] == rl.SETTLED and third["won"] is True


def test_no_payout_for_a_sportsbook_bet_or_without_contracts() -> None:
    games = rf.games(played=LATE)
    book = real.Venue("book", 0.0, 0.01)
    at_book = rl.results(record_on(book), games, resolved())
    assert at_book.filter(pl.col("game_id") == THIRD)["status"].to_list() == [rl.VOID]
    none_held = rl.results(record_on(KALSHI), games, resolved(contracts=0.0, revenue=0))
    assert THIRD not in none_held["game_id"].to_list()


def test_a_payout_beyond_a_contracts_dollar_is_refused() -> None:
    games = rf.games(played=LATE)
    settled = rl.settle(
        record_on(KALSHI), games, rl.settlements(rf.alerted_store()), NIGHT, rf.CODE, resolved()
    )
    beyond = settled.with_columns(
        profit=pl.when(pl.col("game_id") == THIRD).then(3.0).otherwise(pl.col("profit"))
    )
    with pytest.raises(pandera.errors.SchemaError, match="the_profit_follows_the_result"):
        RealSettlements.validate(beyond)


def test_kalshis_settlement_is_kept_once_raw_with_its_first_read() -> None:
    store = rf.alerted_store()
    theirs = kalshi_settled("scalar", revenue=37)[0]
    assert rl.write_exchange_settlement(store, pf.DAY, THIRD, WSH, theirs, NIGHT)
    later = theirs | {"revenue": 40}
    assert not rl.write_exchange_settlement(
        store, pf.DAY, THIRD, WSH, later, NIGHT + timedelta(days=1)
    )
    kept = json.loads(store.get(rl.exchange_settlement_key(pf.DAY, THIRD)))
    assert kept == {"ticker": WSH, "observed_utc": NIGHT.isoformat(), "settlement": theirs}
    read = rl.exchange_settlements(store).row(0, named=True)
    assert read == {
        "game_date": pf.DAY,
        "game_id": THIRD,
        "market_result": "scalar",
        "contracts": 1.0,
        "revenue": 37,
        "observed_utc": NIGHT,
    }


@pytest.mark.parametrize("how", ["never played", "played 15 days late"])
def test_the_check_keeps_kalshis_payout_waits_a_night_then_agrees(tmp_path: Path, how: str) -> None:
    record, exchange, client, clock = finished(tmp_path)
    exchange.settled[WSH] = kalshi_settled("scalar", revenue=37)
    if how == "never played":
        games = rf.games().filter(pl.col("game_id") != FIRST)
    else:
        games = rf.games(played={FIRST: pf.GAMES[FIRST][2] + timedelta(days=15)})
    none = rl.settlements(lb.R2Store(ConditionalBucket(), "bucket"))
    first = pm.check_settlements(record, client, none, date(2026, 10, 23), games)
    assert (first.waiting, first.differences) == (1, [])
    kept = rl.exchange_settlements(record)
    assert kept["game_id"].to_list() == [FIRST] and kept["observed_utc"][0] == clock.at
    # The next nhl real settle pays it out, from that first read on.
    bets = rl.bets(rl.alerts(record), rl.entries(record))
    paid = rl.settle(bets, games, none, clock.at + timedelta(hours=1), rf.CODE, kept)
    row = paid.filter(pl.col("game_id") == FIRST).row(0, named=True)
    assert row["status"] == rl.PAYOUT and row["result_utc"] == clock.at
    again = pm.check_settlements(record, client, paid, date(2026, 10, 24), games)
    assert (again.agreed, again.differences) == (1, [])
    # Kalshi paying other than the record's payout is a difference for the owner.
    exchange.settled[WSH] = kalshi_settled("scalar", revenue=40)
    changed = pm.check_settlements(record, client, paid, date(2026, 10, 24), games)
    assert len(changed.differences) == 1
    assert "Kalshi paid 40¢ for 1.00 contracts; the record's payout is" in changed.differences[0]


def test_the_payout_migration_allows_the_schemas_statuses() -> None:
    import re

    from nhl_edge import cli

    migration = Path("supabase/migrations", cli.PAYOUT_MIGRATION).read_text()
    allowed = re.search(r"status in \(([^)]*)\)\s*\),", migration)
    assert allowed is not None
    statuses = {s.strip().strip("'") for s in allowed.group(1).split(",")}
    assert statuses == {rl.SETTLED, rl.VOID, rl.CANCELLED, rl.PAYOUT}
    first = Path("supabase/migrations/20261016150000_real_record.sql").read_text()
    # It drops the checks it widens, by the names they have: the column's own and the profit's.
    assert "status text not null check (" in first
    assert "constraint real_settlements_profit_follows_the_result check" in first
    for check in cli.PAYOUT_CHECKS:
        assert f"drop constraint {check}" in migration


def test_nhl_real_settle_sends_a_payout_apart_before_its_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nhl_edge import cli
    from nhl_edge.backtest import reports
    from nhl_edge.lake import supabase as sb

    store = lb.R2Store(ConditionalBucket(), "bucket")
    rows = rf.alert_rows(REAL_DAY, venue=KALSHI)
    store.put(rl.alerts_key(pf.DAY), json.dumps({"rows": rl.jsonable(rows)}).encode())
    entries = [rf.entry(FIRST), rf.entry(SECOND, price=1.9, stake=1.0), rf.entry(THIRD, price=2.5)]
    rl.store_entries(store, entries)
    theirs = kalshi_settled("scalar", revenue=37)[0]
    rl.write_exchange_settlement(store, pf.DAY, THIRD, WSH, theirs, NIGHT)

    class Lake:
        def pull(self, table: str, seasons: Any = None) -> int:
            return 0

        def read(self, table: str, seasons: Any = None) -> pl.DataFrame:
            return rf.games(played=LATE)

        def replace(self, table: str, frame: pl.DataFrame) -> None:
            pass

    sent: list[dict[str, Any]] = []

    class Dashboard:
        def insert_new_records(self, table: str, records: list[Any], key: Any) -> int:
            if any(row["status"] == rl.PAYOUT for row in records):
                raise sb.SupabaseError(
                    'insert into real_settlements returned 400: {"code":"23514","message":"new '
                    'row violates check constraint \\"real_settlements_status_check\\""}'
                )
            sent.extend(records)
            return len(records)

    monkeypatch.setattr(cli, "_real_record", lambda r2: (Lake(), store))
    monkeypatch.setattr(sb.Supabase, "from_env", classmethod(lambda cls: Dashboard()))
    monkeypatch.setattr(reports, "version", lambda component, now: "real-settle-20261023-abc1234")
    result = runner.invoke(app, ["real", "settle", "--r2", "--supabase"])
    assert result.exit_code == 0, result.output
    assert "real_settlements: 3 appended, 3 in all" in result.output
    assert f"apply supabase/migrations/{cli.PAYOUT_MIGRATION}" in result.output
    assert sorted(row["game_id"] for row in sent) == [FIRST, SECOND]
    paid = rl.settlements(store).filter(pl.col("game_id") == THIRD)
    assert paid["status"].to_list() == [rl.PAYOUT]


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
    report = pm.check_settlements(
        record, client, rl.settlements(record), date(2026, 10, 9), rf.games()
    )
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
    report = pm.check_settlements(
        record, client, rl.settlements(record), date(2026, 10, 9), rf.games()
    )
    assert report.agreed == 0 and len(report.differences) == 1
    assert report.differences[0].startswith("2026-10-07 WSH: ") and found in report.differences[0]
    message = pm.check_message(report)
    assert message is not None and found in message


def test_a_settlement_missing_waits_two_days_then_is_a_difference(tmp_path: Path) -> None:
    record, _, client, _ = finished(tmp_path)
    soon = pm.check_settlements(
        record, client, rl.settlements(record), date(2026, 10, 8), rf.games()
    )
    assert soon.waiting == 1 and not soon.differences
    late = pm.check_settlements(
        record, client, rl.settlements(record), date(2026, 10, 9), rf.games()
    )
    assert late.differences == ["2026-10-07 WSH: not settled by Kalshi yet"]


def test_a_postponed_game_waits_from_the_day_it_is_played(tmp_path: Path) -> None:
    # WSH's game moved three days (Codex on #291): unplayed, neither side has settled it and it
    # waits however late the check; once played, the two days count from then.
    record, _, client, _ = finished(tmp_path)
    unsettled = rl.settlements(lb.R2Store(ConditionalBucket(), "bucket"))
    unplayed = rf.games().filter(pl.col("game_id") != FIRST)
    check = pm.check_settlements(record, client, unsettled, date(2026, 10, 9), unplayed)
    assert check.waiting == 1 and not check.differences
    final = rf.games(played={FIRST: pf.GAMES[FIRST][2] + timedelta(days=3)})
    soon = pm.check_settlements(record, client, unsettled, date(2026, 10, 11), final)
    assert soon.waiting == 1 and not soon.differences
    late = pm.check_settlements(record, client, unsettled, date(2026, 10, 12), final)
    assert late.differences == ["2026-10-07 WSH: not settled by Kalshi yet"]


def test_a_payout_the_record_leaves_unsettled_after_its_first_night_is_a_difference(
    tmp_path: Path,
) -> None:
    # A game cancelled, or one the lake missed: Kalshi resolved it at a fair price. Its first
    # night it waits for nhl real settle to pay it out (#290); a record still without it the
    # next night counts from the game's listed date.
    record, exchange, client, _ = finished(tmp_path)
    exchange.settled[WSH] = kalshi_settled("scalar", revenue=37)
    unsettled = rl.settlements(lb.R2Store(ConditionalBucket(), "bucket"))
    unplayed = rf.games().filter(pl.col("game_id") != FIRST)
    soon = pm.check_settlements(record, client, unsettled, date(2026, 10, 8), unplayed)
    assert soon.waiting == 1 and not soon.differences
    late = pm.check_settlements(record, client, unsettled, date(2026, 10, 9), unplayed)
    assert late.differences == ["2026-10-07 WSH: not settled by the record yet"]


def test_orders_without_fills_or_long_past_are_not_checked(tmp_path: Path) -> None:
    record, _, client, _ = finished(tmp_path, fill=None)
    assert (
        pm.check_settlements(
            record, client, rl.settlements(record), date(2026, 10, 9), rf.games()
        ).checked
        == 0
    )
    record, _, client, _ = finished(tmp_path / "old")
    old = date(2026, 10, 7) + timedelta(days=pm.CHECK_DAYS + 1)
    assert (
        pm.check_settlements(record, client, rl.settlements(record), old, rf.games()).checked == 0
    )


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


def unreadable_live_key(cls: Any, env: str, raw: Any) -> Any:
    return kt.load_key("pasted-wrong", "KALSHI_PRIVATE_KEY")


def test_nhl_real_reconcile_kalshi_warns_of_a_key_with_no_live_order_to_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge import cli

    local_raw(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli, "_real_record", lambda r2: (None, lb.R2Store(ConditionalBucket(), "b"))
    )
    monkeypatch.setattr(kt.KalshiTrade, "from_env", classmethod(unreadable_live_key))
    result = runner.invoke(app, ["real", "reconcile-kalshi", "--r2"])
    assert result.exit_code == 0, result.output
    assert "::warning::kalshi settlements: KALSHI_PRIVATE_KEY is no unencrypted" in result.output
    assert "no live order to check" in result.output


def test_nhl_real_reconcile_kalshi_fails_on_a_key_a_live_order_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge import cli

    record, *_ = finished(tmp_path)
    local_raw(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_real_record", lambda r2: (None, record))
    monkeypatch.setattr(kt.KalshiTrade, "from_env", classmethod(unreadable_live_key))
    result = runner.invoke(app, ["real", "reconcile-kalshi", "--r2"])
    assert result.exit_code == 1, result.output
    assert "kalshi settlements: failed (KALSHI_PRIVATE_KEY is no unencrypted" in result.output


def test_nhl_real_reconcile_kalshi_sends_the_differences_and_logs_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge import cli
    from nhl_edge.live import telegram

    record, exchange, client, _ = finished(tmp_path)
    exchange.settled[WSH] = kalshi_settled("no", revenue=0)
    sent: list[str] = []
    local_raw(tmp_path, monkeypatch)

    class Lake:
        def __init__(self) -> None:
            self.pulled: list[str] = []

        def pull(self, table: str, seasons: Any = None) -> int:
            self.pulled.append(table)
            return 0

        def read(self, table: str, seasons: Any = None) -> pl.DataFrame:
            return rf.games()

    lake = Lake()
    monkeypatch.setattr(cli, "_real_record", lambda r2: (lake, record))
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
    assert lake.pulled == ["games"]


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
