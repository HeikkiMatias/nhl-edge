"""Real stakes day by day (live/real_ledger.py, #268, ADR 0039): the minimum price and the stake
table, the day's mode, the alert and its message, the owner's entries and corrections, the
bankroll and settlement."""

import json
from datetime import UTC, date, datetime, timedelta

import httpx
import polars as pl
import predict_fixtures as pf
import pytest
import real_fixtures as rf
from fakes import ConditionalBucket

from nhl_edge.betting.selection import POLICY_VERSION
from nhl_edge.live import bundle as lb
from nhl_edge.live import real, telegram
from nhl_edge.live import real_ledger as rl

FIRST, SECOND, THIRD = pf.GAMES


def store() -> lb.R2Store:
    return lb.R2Store(ConditionalBucket(), "bucket")


def test_the_minimum_price_is_the_lowest_cent_whose_return_clears_the_hurdle() -> None:
    assert rl.min_price(0.5, 0.025, 2.20) == 2.05
    # At 2.05 the return equals the hurdle (to rounding), at 2.04 it falls short.
    assert 0.5 * 2.05 - 1 >= 0.025 - 1e-9 and 0.5 * 2.04 - 1 < 0.025
    # Never above the paper price, at which the return did clear it.
    assert rl.min_price(1.025 / 1.9165, 0.025, 1.917) == 1.917


def test_a_stake_is_rounded_down_and_none_below_the_venue_minimum() -> None:
    assert rl.round_down(1.3887, real.PRACTICE_VENUE) == 1.38
    assert rl.round_down(1.3887, real.Venue("v", 0.0, 0.10)) == 1.3
    assert rl.round_down(1.3887, real.Venue("v", 1.39, 0.01)) == 0.0
    assert rl.round_down(0.004, real.PRACTICE_VENUE) == 0.0


def test_the_stake_table_never_exceeds_the_paper_stake_and_ends_at_the_paper_price() -> None:
    paper = rf.ledger().filter(pl.col("bet"))
    rows = rf.alert_rows()
    for bet in paper.iter_rows(named=True):
        table = rf.table_of(rows, bet["game_id"])
        prices = [price for price, _ in table]
        stakes = [stake for _, stake in table]
        assert prices[-1] == bet["price"] and prices == sorted(prices)
        assert stakes == sorted(stakes), "a better price never stakes less"
        assert max(stakes) <= bet["fraction"] * 100 + 1e-9
        # Every listed price clears the hurdle (hard rule 4).
        assert all(bet["p_side"] * price - 1 >= bet["hurdle"] - 1e-9 for price in prices)
    first = rows.filter(pl.col("game_id") == FIRST).row(0, named=True)
    assert first["max_stake"] == 1.5 and first["min_price"] == rf.table_of(rows, FIRST)[0][0]


def test_a_venue_minimum_shortens_the_table_or_skips_the_bet() -> None:
    table = rf.table_of(rf.alert_rows(venue=real.Venue("v", 1.0, 0.01)), FIRST)
    assert min(stake for _, stake in table) >= 1.0
    skipped = rf.alert_rows(venue=real.Venue("v", 2.0, 0.01))
    assert set(skipped["status"]) == {rl.SKIP_MINIMUM}
    assert set(skipped["max_stake"]) == {0.0}


def test_the_day_cap_holds_the_other_bets_at_their_paper_prices() -> None:
    # Five bets at the full 1.5% make 7.5%, scaled to the 5% day cap: 1% each. A lower price
    # for one bet can only lower its own stake below that, never raise it.
    bets = pl.DataFrame(
        {
            "season": [20262027] * 5,
            "game_date": [pf.DAY] * 5,
            "game_id": list(range(1, 6)),
            "price": [2.4] * 5,
            "p_side": [0.5] * 5,
            "u_sd": [0.0] * 5,
            "fraction": [0.01] * 5,
            "hurdle": [0.025] * 5,
        }
    )
    tables = rl.stake_tables(bets, 100.0, real.PRACTICE_VENUE)
    for table in tables.values():
        assert table[-1] == (2.4, 1.0)
        assert all(stake <= 1.0 for _, stake in table)


def test_the_mode_follows_the_weekly_reports_and_the_real_bankroll() -> None:
    nov = {d: date(2026, 11, d) for d in (9, 16, 17, 23, 30)}
    holds = rf.report(nov[16], "holds", stop=False, pause=False)
    assert rl.mode([], nov[17], 100.0).name == rl.PRACTICE
    assert rl.mode([rf.report(nov[16])], nov[17], 100.0).name == rl.PRACTICE
    started = rl.mode([holds], nov[16], 100.0)
    assert (started.name, started.started) == (rl.REAL, nov[16])
    # A later failing report doesn't undo a start: the stops decide from then on.
    assert rl.mode([holds, rf.report(nov[23], stop=False, pause=False)], nov[23], 100.0).name == (
        rl.REAL
    )
    stopped = rl.mode([holds, rf.report(nov[23], stop=True, pause=False)], nov[30], 100.0)
    assert stopped.name == rl.STOPPED and "2026-11-23" in stopped.reason
    paused = [holds, rf.report(nov[23], stop=False, pause=True)]
    assert rl.mode(paused, nov[23], 100.0).name == rl.PAUSED
    resumed = [*paused, rf.report(nov[30], stop=False, pause=False)]
    assert rl.mode(resumed, nov[30], 100.0).name == rl.REAL
    # The 20% stop, at its boundary.
    assert rl.mode([holds], nov[17], 80.0).name == rl.STOPPED
    assert rl.mode([holds], nov[17], 80.01).name == rl.REAL
    # Without a start by the window's end it closes; a start inside it carries on after it.
    assert rl.mode([rf.report(nov[30])], date(2026, 12, 1), 100.0).name == rl.CLOSED
    assert rl.mode([rf.report(nov[30], "holds")], date(2026, 12, 7), 100.0).name == rl.REAL


def test_only_the_frozen_policys_weekly_reports_are_read(tmp_path: object) -> None:
    from pathlib import Path

    folder = Path(str(tmp_path))
    weekly = rf.report(date(2026, 11, 16), "holds")
    (folder / "report-2026-11-16.json").write_text(json.dumps(weekly))
    other = rf.report(date(2026, 11, 23), "holds") | {"policy_version": "policy-x"}
    (folder / "report-2026-11-23.json").write_text(json.dumps(other))
    nightly = rf.report(date(2026, 11, 18), "holds")
    nightly["real_stakes"]["weekly"] = False
    (folder / "report-2026-11-18.json").write_text(json.dumps(nightly))
    (folder / f"report-2026-11-16-{POLICY_VERSION}.json").write_text(json.dumps(weekly))
    assert [r["as_of"] for r in rl.weekly_reports(folder)] == ["2026-11-16"]


def test_a_paused_or_stopped_day_alerts_nothing() -> None:
    for name in (rl.PAUSED, rl.STOPPED, rl.CLOSED):
        rows = rf.alert_rows(rl.Mode(name, date(2026, 11, 16), "why"))
        assert set(rows["status"]) == {rl.SKIP_MODE[name]}
    assert rf.alert_rows(now=pf.START).filter(pl.col("game_id") == FIRST)["status"].item() == (
        rl.SKIP_STARTED
    )
    no_bets = rf.ledger().with_columns(bet=pl.lit(False))
    empty = rl.alert_rows(no_bets, rl.Mode(rl.PRACTICE), 100.0, real.PRACTICE_VENUE, rf.ALERT, "x")
    assert empty.is_empty()


def test_the_message_lists_each_bet_or_says_why_there_is_none() -> None:
    rows = rf.alert_rows()
    text = rl.message(pf.DAY, rl.Mode(rl.PRACTICE), rows, 100.0, "https://dash.test")
    assert text.startswith("NHL Edge · PRACTICE, don't bet · Wed 7 Oct\nPractice bankroll 100.00")
    assert "PIT at WSH, starts Thu 02:30 · bet WSH" in text
    assert "  stake: 1.50 € at 2.06 or more · " in text and "Record each bet" in text
    assert "dash.test" in text and "Practice only" in text
    real_rows = rf.alert_rows(rl.Mode(rl.REAL, date(2026, 11, 16)))
    assert rl.message(pf.DAY, rl.Mode(rl.REAL), real_rows, 100.0, None).startswith(
        "NHL Edge · real bets · Wed 7 Oct\nReal bankroll 100.00 €"
    )
    stopped = rl.Mode(rl.STOPPED, date(2026, 11, 16), "the real bankroll is 79.50 €")
    text = rl.message(pf.DAY, stopped, rf.alert_rows(stopped), 79.5, None)
    assert "Real bets are stopped: the real bankroll is 79.50 €." in text and "PIT" not in text
    none = rl.message(pf.DAY, rl.Mode(rl.REAL), rows.clear(), 100.0, None, "no midday snapshot")
    assert "No real bets today." in none and "skipped today (no midday snapshot)" in none
    closed = rl.message(date(2026, 12, 1), rl.Mode(rl.CLOSED, reason="r"), rows.clear(), 100, None)
    assert "Real stakes didn't start: r." in closed


def test_the_ladder_gives_each_stake_its_lowest_price() -> None:
    table = [
        (2.0, 0.5),
        (2.01, 0.7),
        (2.02, 0.7),
        (2.03, 0.9),
        (2.04, 1.1),
        (2.05, 1.5),
        (2.1, 1.5),
    ]
    assert rl.ladder(table) == [(2.05, 1.5), (2.04, 1.1), (2.01, 0.7), (2.0, 0.5)]
    assert rl.ladder(table[:2]) == [(2.01, 0.7), (2.0, 0.5)]
    assert rl.stake_at(table, 2.025) == 0.7 and rl.stake_at(table, 1.99) == 0.0
    assert rl.stake_at(table, 3.0) == 1.5


def test_a_day_is_alerted_once_and_its_message_kept() -> None:
    bucket = store()
    alert = rl.prepare(pf.DAY, rf.ledger(), [], rl.bets(rf.alert_rows().clear(), []), rf.games(),
                       rf.ALERT, rf.CODE, "https://dash.test")  # fmt: skip
    assert rl.alerted(bucket, pf.DAY) is None
    rl.write_alert(bucket, alert)
    with pytest.raises(ValueError, match="already alerted"):
        rl.write_alert(bucket, alert)
    record = rl.alerted(bucket, pf.DAY)
    assert record is not None and record["message"] == alert.message
    assert record["mode"] == rl.PRACTICE and record["bankroll"] == 100.0
    read = rl.alerts(bucket)
    assert read.equals(alert.rows)
    assert not rl.closed_already(bucket)


def test_entries_are_copied_once_and_never_edited() -> None:
    bucket = store()
    assert rl.store_entries(bucket, [rf.entry()]) == 1
    assert rl.store_entries(bucket, [rf.entry()]) == 0
    with pytest.raises(lb.Conflict):
        rl.store_entries(bucket, [rf.entry(stake=1.4)])
    assert rl.entries(bucket)[0]["bet_id"] == f"B{FIRST}"


def test_every_alert_row_reads_placed_not_placed_or_skipped() -> None:
    rows = rf.alert_rows(now=datetime(2026, 10, 8, 0, tzinfo=UTC))  # the 23:30 games started
    placed = datetime(2026, 10, 8, 0, 30, tzinfo=UTC)  # after the alert, before THIRD's start
    record = rl.bets(rows, [rf.entry(THIRD, price=2.40, stake=1.0, placed=placed, entered=placed)])
    by_game = {row["game_id"]: row for row in record.iter_rows(named=True)}
    assert by_game[FIRST]["outcome"] == rl.SKIPPED
    third = by_game[THIRD]
    assert (third["outcome"], third["late"], third["corrections"]) == (rl.PLACED, False, 0)
    assert third["allowed_stake"] == rl.stake_at(rf.table_of(rows, THIRD), 2.40)
    assert third["within_rule"] is (third["allowed_stake"] >= 1.0)
    unrecorded = rl.bets(rf.alert_rows(), [])
    assert set(unrecorded["outcome"]) == {rl.NOT_PLACED}
    with pytest.raises(ValueError, match="wasn't alerted"):
        rl.bets(rows, [rf.entry(FIRST)])
    with pytest.raises(ValueError, match="without an alert"):
        rl.bets(rows, [rf.entry(FIRST) | {"game_date": "2026-10-06"}])


def test_a_stake_over_the_rule_or_a_price_under_the_minimum_is_flagged_not_refused() -> None:
    rows = rf.alert_rows()
    over = rl.bets(rows, [rf.entry(FIRST, price=2.0, stake=1.5)]).filter(pl.col("game_id") == FIRST)
    assert over["within_rule"].item() is False and over["outcome"].item() == rl.PLACED
    low = rl.bets(rows, [rf.entry(FIRST, price=1.95, stake=0.1)]).filter(pl.col("game_id") == FIRST)
    assert low["within_rule"].item() is False


def test_corrections_are_appended_and_applied_in_order() -> None:
    bucket = rf.alerted_store()
    rows = rl.alerts(bucket)
    rl.store_entries(bucket, [rf.entry(FIRST, stake=1.0)])
    now = datetime(2026, 10, 12, 10, tzinfo=UTC)
    rl.correct(bucket, pf.DAY, FIRST, {"stake": 1.2}, "the statement says 1.20", now)
    rl.correct(bucket, pf.DAY, FIRST, {"stake": 1.3}, "and then 1.30", now + timedelta(minutes=1))
    # A bet placed but never recorded is added at the weekly check, marked late.
    forgot = {
        "outcome": "placed",
        "matched_price": 1.9,
        "stake": 1.0,
        "bet_id": "X1",
        "placed_utc": "2026-10-07T17:30:00+00:00",
    }
    rl.correct(bucket, pf.DAY, SECOND, forgot, "on the statement, not recorded", now)
    record = rl.bets(rows, rl.entries(bucket), rl.corrections(bucket))
    first = record.filter(pl.col("game_id") == FIRST).row(0, named=True)
    assert (first["stake"], first["corrections"]) == (1.3, 2)
    second = record.filter(pl.col("game_id") == SECOND).row(0, named=True)
    assert (second["outcome"], second["late"], second["bet_id"]) == (rl.PLACED, True, "X1")
    with pytest.raises(ValueError, match="changes some of"):
        rl.correct(bucket, pf.DAY, FIRST, {"side": "away"}, "no", now)
    with pytest.raises(ValueError, match="its reason"):
        rl.correct(bucket, pf.DAY, FIRST, {"stake": 1.0}, " ", now)
    # One that would leave a placed bet without its bet id is refused, and nothing is written.
    held = len(rl.corrections(bucket))
    with pytest.raises(ValueError):
        rl.correct(bucket, pf.DAY, THIRD, {"outcome": "placed"}, "incomplete", now)
    assert len(rl.corrections(bucket)) == held


def test_the_bankroll_counts_only_its_own_modes_placed_bets() -> None:
    rows = rf.alert_rows()
    record = rl.bets(rows, [rf.entry(FIRST, price=2.10, stake=1.5), rf.entry(SECOND, stake=1.0,
                     price=1.9)])  # fmt: skip
    after = datetime(2026, 10, 8, 16, 47, tzinfo=UTC)
    # FIRST won at home (+1.65), SECOND lost at home (-1.00).
    assert rl.bankroll(record, rf.games(), after, rl.PRACTICE) == pytest.approx(100.65)
    assert rl.bankroll(record, rf.games(), after, rl.REAL) == 100.0


def test_settlement_is_on_the_full_game_appended_once_and_revised_by_a_correction() -> None:
    bucket = rf.alerted_store()
    rows = rl.alerts(bucket)
    rl.store_entries(bucket, [rf.entry(FIRST, stake=1.5), rf.entry(THIRD, price=2.5, stake=1.5)])
    # FIRST won in a shootout (the scores count the winning goal), THIRD's game was postponed.
    games = rf.games(
        {FIRST: (4, 3), SECOND: (1, 4), THIRD: (2, 1)},
        played={THIRD: pf.GAMES[THIRD][2] + timedelta(days=2)},
    )
    now = datetime(2026, 10, 10, 9, tzinfo=UTC)
    record = rl.bets(rows, rl.entries(bucket))
    settled = rl.settle(record, games, rl.settlements(bucket), now, rf.CODE)
    by_game = {row["game_id"]: row for row in settled.iter_rows(named=True)}
    assert by_game[FIRST]["won"] and by_game[FIRST]["profit"] == pytest.approx(1.5 * 1.1)
    assert by_game[THIRD]["status"] == rl.VOID and by_game[THIRD]["profit"] == 0.0
    assert by_game[THIRD]["bankroll_after"] == pytest.approx(101.65)
    rl.write_settlements(bucket, settled)
    assert rl.settle(record, games, rl.settlements(bucket), now, rf.CODE).is_empty()
    # The weekly check finds FIRST's stake was 1.00: a second revision, the bankroll after it.
    rl.correct(bucket, pf.DAY, FIRST, {"stake": 1.0}, "statement", now)
    record = rl.bets(rows, rl.entries(bucket), rl.corrections(bucket))
    revised = rl.settle(record, games, rl.settlements(bucket), now, rf.CODE)
    assert revised.select("game_id", "revision").rows() == [(FIRST, 1)]
    assert revised["bankroll_after"].item() == pytest.approx(101.1)
    rl.write_settlements(bucket, revised)
    # A bet found not placed after all is cancelled, with no profit.
    rl.correct(bucket, pf.DAY, FIRST, {"outcome": "not placed", "reason": "not matched"}, "s", now)
    record = rl.bets(rows, rl.entries(bucket), rl.corrections(bucket))
    cancelled = rl.settle(record, games, rl.settlements(bucket), now, rf.CODE)
    assert cancelled.select("status", "revision", "profit", "bankroll_after").rows() == [
        (rl.CANCELLED, 2, 0.0, 100.0)
    ]
    rl.write_settlements(bucket, cancelled)
    assert rl.settlements(bucket).height == 4
    lines = rl.statement(record, rl.settlements(bucket), rl.PRACTICE)
    assert lines[-1] == "practice bankroll from the settlements: 100.00 €"


def test_the_alert_rows_reach_supabase_with_the_stakes_as_a_list() -> None:
    sent = rl.supabase_alerts(rf.alert_rows())
    assert sent[0]["stakes"][-1] == {"price": 2.1, "stake": 1.5}
    assert sent[0]["start_utc"] == "2026-10-07T23:30:00+00:00"


class FakeClient:
    def __init__(self, status: int, body: dict[str, object] | None, fail: bool = False) -> None:
        self.status, self.body, self.fail = status, body, fail
        self.sent: list[dict[str, str]] = []

    def post(self, url: str, data: dict[str, str]) -> httpx.Response:
        if self.fail:
            raise httpx.ConnectError(f"cannot reach {url}")
        self.sent.append(data)
        return httpx.Response(self.status, json=self.body)


def test_telegram_errors_never_show_the_token() -> None:
    ok = FakeClient(200, {"ok": True})
    telegram.send("hi", "TOKEN123", "42", ok)  # type: ignore[arg-type]
    assert ok.sent == [{"chat_id": "42", "text": "hi", "disable_web_page_preview": "true"}]
    for client in (
        FakeClient(0, None, fail=True),
        FakeClient(400, {"ok": False, "description": "x"}),
    ):
        with pytest.raises(telegram.TelegramError) as caught:
            telegram.send("hi", "TOKEN123", "42", client)  # type: ignore[arg-type]
        assert "TOKEN123" not in str(caught.value)
    with pytest.raises(telegram.TelegramError, match="over 4096"):
        telegram.send("x" * 4097, "TOKEN123", "42", ok)  # type: ignore[arg-type]


def test_the_rows_sent_and_read_are_the_migrations_columns() -> None:
    import re
    from pathlib import Path

    migration = Path("supabase/migrations/20261016120000_real_bets.sql").read_text()

    def columns(table: str) -> set[str]:
        body = re.search(rf"create table public\.{table} \((.*?)\n\);", migration, re.S)
        assert body is not None
        types = "bigint|integer|text|date|timestamptz|numeric|boolean|uuid|jsonb"
        column = re.compile(rf"^  ([a-z_0-9]+) (?:{types})\b")
        return {m.group(1) for line in body.group(1).splitlines() if (m := column.match(line))}

    assert set(rl.supabase_alerts(rf.alert_rows())[0]) == columns("real_alerts") - {"created_at"}
    assert set(rl.ENTRY_FIELDS) == columns("real_entries") - {"id", "entered_by"}
    reasons = re.search(r"reason in \((.*?)\)\)", migration)
    assert reasons is not None
    from nhl_edge.lake.schemas import REAL_REASONS

    assert re.findall(r"'([^']+)'", reasons.group(1)) == list(REAL_REASONS)


def test_a_game_starting_before_the_write_is_alerted_no_more() -> None:
    # Built a minute before FIRST's start; by the check after it, FIRST has started, so the day
    # is built again at that reading (Codex on #276).
    readings = iter([pf.START - timedelta(minutes=1), pf.START, pf.START, pf.START])
    empty = rl.bets(rf.alert_rows().clear(), [])

    def build(at: datetime) -> rl.Alert:
        return rl.prepare(pf.DAY, rf.ledger(), [], empty, rf.games(), at, rf.CODE)

    alert = rl.publishable(build, lambda: next(readings))
    status = dict(alert.rows.select("game_id", "status").iter_rows())
    assert status[FIRST] == rl.SKIP_STARTED and status[THIRD] == rl.ALERTED
    assert alert.alert_utc == pf.START


def test_a_stored_alert_reads_back_as_written() -> None:
    bucket = rf.alerted_store()
    record = rl.alerted(bucket, pf.DAY)
    assert record is not None
    assert rl.stored_rows(record).equals(rl.alerts(bucket))


def test_a_correction_never_places_a_bet_before_its_alert() -> None:
    bucket = rf.alerted_store()
    now = datetime(2026, 10, 12, 10, tzinfo=UTC)
    early = {
        "outcome": "placed",
        "matched_price": 2.1,
        "stake": 1.0,
        "bet_id": "X1",
        "placed_utc": (rf.ALERT - timedelta(minutes=1)).isoformat(),
    }
    with pytest.raises(ValueError, match="outside its alert"):
        rl.correct(bucket, pf.DAY, FIRST, early, "on the statement", now)
    assert rl.corrections(bucket) == []
    with pytest.raises(ValueError, match="outside its alert"):
        rl.bets(rl.alerts(bucket), [rf.entry(FIRST, placed=rf.ALERT - timedelta(seconds=1))])


def test_a_resent_alert_never_offers_a_game_that_has_started() -> None:
    # The first send failed; resent after FIRST's start, its game is no bet (Codex on #276).
    record = rl.alerted(rf.alerted_store(), pf.DAY)
    assert record is not None
    assert rl.resend_message(record, rf.later(1), None) == record["message"]
    text = rl.resend_message(record, pf.START + timedelta(minutes=5), None)
    assert "PIT at WSH, starts Thu 02:30: no bet (the game started before the alert)" in text
    assert "EDM at ANA, starts Thu 05:00 · bet ANA" in text
    assert text.endswith("(Sent again at 02:35.)")


def test_a_stop_holds_until_the_owner_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Once a day was alerted as stopped, a later result or correction lifting the bankroll above
    # 80 € doesn't restart real bets (Codex on #276); only the owner's restart does.
    holds = [rf.report(date(2026, 11, 16), "holds", stop=False, pause=False)]
    day = date(2026, 11, 20)
    stopped = rl.mode(holds, day, 100.0, [date(2026, 11, 18)])
    assert (stopped.name, stopped.reason) == (rl.STOPPED, "real bets were stopped on 18 Nov")
    # A stop recorded before the start (impossible as alerts go) or on the day itself is not one.
    assert rl.mode(holds, day, 100.0, [date(2026, 11, 10), day]).name == rl.REAL
    monkeypatch.setattr(real, "RESTARTED_ON", date(2026, 11, 19))
    assert rl.mode(holds, day, 100.0, [date(2026, 11, 18)]).name == rl.REAL


def test_the_statement_reads_each_bets_settlement_or_awaits_it() -> None:
    bucket = rf.alerted_store()
    rl.store_entries(bucket, [rf.entry(FIRST, stake=1.5), rf.entry(THIRD, price=2.5, stake=1.0)])
    record = rl.bets(rl.alerts(bucket), rl.entries(bucket))
    # Only FIRST's game is final.
    games = rf.games({FIRST: (3, 2)})
    settled = rl.settle(record, games, rl.settlements(bucket), rf.later(20), rf.CODE)
    lines = rl.statement(record, settled, rl.PRACTICE)
    assert "settled, +1.65 €, bankroll 101.65 €" in lines[0]
    assert lines[1].endswith(": awaiting the result")
    assert lines[-1] == "practice bankroll from the settlements: 101.65 €"


def test_the_settlements_sent_are_the_real_settlements_migrations_columns() -> None:
    import re
    from pathlib import Path

    migration = Path("supabase/migrations/20261016150000_real_settlements.sql").read_text()
    body = re.search(r"create table public\.real_settlements \((.*?)\n\);", migration, re.S)
    assert body is not None
    types = "bigint|integer|smallint|text|date|timestamptz|numeric|boolean|double precision"
    column = re.compile(rf"^  ([a-z_0-9]+) (?:{types})\b")
    found = {m.group(1) for line in body.group(1).splitlines() if (m := column.match(line))}
    bucket = rf.alerted_store()
    rl.store_entries(bucket, [rf.entry(FIRST, stake=1.5)])
    record = rl.bets(rl.alerts(bucket), rl.entries(bucket))
    settled = rl.settle(record, rf.games(), rl.settlements(bucket), rf.later(20), rf.CODE)
    sent = rl.supabase_settlements(settled)
    assert set(sent[0]) == found - {"created_at"}
    assert sent[0]["result_utc"] == "2026-10-08T06:00:00+00:00"
    assert re.search(r"primary key \((.*?)\)", body.group(1)).group(1).split(", ") == list(  # type: ignore[union-attr]
        rl.SETTLEMENT_KEY
    )
