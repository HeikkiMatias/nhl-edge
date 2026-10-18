"""Point-in-time rules for real stakes (#268, ADR 0039, hard rule 1): a day's alert reads only what
was public at its own clock, never only at the game's. Its mode reads only the weekly reports dated
on or before the day; its bankroll only the results public before the decision; a game already
under way at the alert gets no alert; and settlement counts only results public by its clock."""

from datetime import UTC, date, datetime, timedelta

import polars as pl
import predict_fixtures as pf
import real_fixtures as rf
from fakes import ConditionalBucket

from nhl_edge.live import bundle as lb
from nhl_edge.live import real
from nhl_edge.live import real_ledger as rl

FIRST = next(iter(pf.GAMES))


def test_the_mode_never_reads_a_later_weekly_report() -> None:
    # The report of 11-23 says the rule holds; on 11-20 the start hasn't happened yet.
    later = [rf.report(date(2026, 11, 16)), rf.report(date(2026, 11, 23), "holds")]
    assert rl.mode(later, date(2026, 11, 20), 100.0).name == rl.PRACTICE
    assert rl.mode(later, date(2026, 11, 23), 100.0).name == rl.REAL
    # A stop dated after the day doesn't stop it.
    stop = [rf.report(date(2026, 11, 16), "holds"), rf.report(date(2026, 11, 23), stop=True)]
    assert rl.mode(stop, date(2026, 11, 22), 100.0).name == rl.REAL


def test_the_bankroll_counts_only_results_public_before_the_decision() -> None:
    record = rl.bets(rf.alert_rows(), [rf.entry(FIRST, stake=1.5)])
    public = datetime(2026, 10, 8, 6, tzinfo=UTC)
    games = rf.games(observed=public)
    assert rl.bankroll(record, games, public, rl.PRACTICE) == 100.0
    after = rl.bankroll(record, games, public + timedelta(seconds=1), rl.PRACTICE)
    assert after > 100.0


def test_a_game_under_way_at_the_alert_gets_no_alert() -> None:
    start = pf.GAMES[FIRST][2]
    for now, expected in ((start - timedelta(seconds=1), rl.ALERTED), (start, rl.SKIP_STARTED)):
        rows = rf.alert_rows(now=now)
        assert rows.filter(pl.col("game_id") == FIRST)["status"].item() == expected


def test_settlement_counts_only_results_public_by_its_clock() -> None:
    record = rl.bets(rf.alert_rows(), [rf.entry(FIRST, stake=1.5)])
    public = datetime(2026, 10, 8, 6, tzinfo=UTC)
    games = rf.games(observed=public)
    empty = rl.settle(record, games, rl.settlements(_store()), public - timedelta(seconds=1), "v")
    assert empty.is_empty()
    assert rl.settle(record, games, rl.settlements(_store()), public, "v").height == 1


def _store() -> lb.R2Store:
    return lb.R2Store(ConditionalBucket(), "bucket")


def test_a_late_kalshi_game_enters_the_bankroll_only_once_its_result_is_public() -> None:
    # On an exchange venue a game played days late settles on its result (ADR 0040, #287), so
    # its bet counts once that result is public, not before; one played over two weeks late is
    # held for Kalshi's own resolution and never counts here.
    kalshi = real.Venue("kalshi", 0.0, 0.01, real.Exchange("live", 1.16))
    rows = rf.alert_rows(rl.Mode(rl.REAL, rf.ALERT.date()), venue=kalshi)
    record = rl.bets(rows, [rf.entry(FIRST, stake=1.5)])
    for late, counts in ((timedelta(days=3), True), (timedelta(days=15), False)):
        played = pf.GAMES[FIRST][2] + late
        public = played + timedelta(hours=6)
        games = rf.games(observed=public, played={FIRST: played})
        assert rl.bankroll(record, games, public, rl.REAL) == 100.0
        after = rl.bankroll(record, games, public + timedelta(seconds=1), rl.REAL)
        assert (after > 100.0) is counts
