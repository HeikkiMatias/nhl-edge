"""ADR 0047 (#322): the real bankroll grows only on one reading of 12 weeks of real fills, taken
as of the weekly report's clock, written once, and kept private."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from nhl_edge.live import bundle as lb
from nhl_edge.live import fills as lf
from nhl_edge.live import scale_up as su
from nhl_edge.market import closing

FIRST = date(2026, 11, 18)  # a Wednesday: its week's Monday is 2026-11-16
READING = date(2027, 2, 8)
CLOCK = datetime(2027, 2, 8, 10, tzinfo=UTC)
# The lake's odds, unread: the fixture below gives the references.
NO_ODDS = pl.DataFrame(schema={"snapshot_utc": pl.Datetime("us", "UTC")})


def found(
    weeks: int, kinds: tuple[str, ...] = (lf.TAKER, lf.MAKER), start: date = FIRST
) -> pl.DataFrame:
    """One real fill a day of each kind from start to the end of the weeks-th Monday-to-Sunday
    week, each its own game at 00:00 UTC the next day, placed at 17:00 UTC and filled two hours
    later."""
    end = start - timedelta(days=start.weekday()) + timedelta(weeks=weeks)
    days = [start + timedelta(days=d) for d in range((end - start).days)]
    rows = []
    for i, day in enumerate(days):
        for kind in kinds:
            placed = datetime(day.year, day.month, day.day, 17, tzinfo=UTC)
            rows.append(
                {
                    "game_date": day,
                    "season": 20262027,
                    "game_id": 2026020100 + i,
                    "team": "WSH",
                    "start_utc": placed + timedelta(hours=7),
                    "placed_utc": placed,
                    "fill_utc": placed + timedelta(hours=2),
                    "kind": kind,
                    "contracts": 1.0,
                    "cost_usd": 0.5,
                }
            )
    return pl.DataFrame(rows, schema=lf.FILL_SCHEMA)


def refs(games: pl.DataFrame, p_close_home: float = 0.55) -> pl.DataFrame:
    """Each game's Pinnacle proxy 45 minutes before its start, at a fixed fair price."""
    return games.select(
        "game_id",
        close_status=pl.lit(closing.PROXY),
        close_snapshot_utc=pl.col("start_utc") - timedelta(minutes=45),
        p_close_home=pl.lit(p_close_home),
    ).unique("game_id")


def teams(games: pl.DataFrame) -> pl.DataFrame:
    return games.select("game_id", home=pl.lit("WSH"), away=pl.lit("PIT")).unique("game_id")


def referenced(fills: pl.DataFrame, now: datetime, p_close_home: float = 0.55) -> pl.DataFrame:
    return lf.referenced(fills, refs(fills, p_close_home), teams(fills), now)


@pytest.fixture
def references(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """The reading's references and teams from the fills it reads, at a settable fair price, in
    place of the lake's odds and the alerts."""
    price = {"p_close_home": 0.55}
    original = lf.referenced

    def fake(
        fills: pl.DataFrame, _refs: pl.DataFrame, _teams: pl.DataFrame, now: datetime
    ) -> pl.DataFrame:
        return original(fills, refs(fills, price["p_close_home"]), teams(fills), now)

    monkeypatch.setattr(lf, "references", lambda odds, now: pl.DataFrame())
    monkeypatch.setattr(lf, "referenced", fake)
    return price


def test_the_reading_is_on_the_monday_twelve_weeks_after_the_first_fills_week() -> None:
    assert su.reading_date(found(1)) == READING
    assert su.reading_date(found(1, start=date(2026, 11, 16))) == READING
    assert su.reading_date(found(1).head(0)) is None
    # Its clock is the weekly report's, once Sunday's results are public.
    assert su.clock(READING) == CLOCK


def test_twelve_weeks_of_positive_clv_with_every_fill_referenced_pass() -> None:
    sample = su.figures(referenced(found(12), CLOCK))[lf.TAKER]
    assert sample["weeks"] == 12 and sample["coverage"] == 1.0
    # p 0.55 for a contract bought at 0.50: CLV +10% on every bid.
    assert sample["clv_per_bid"]["low"] == pytest.approx(0.1)
    assert su.failing(sample, stopped=False) == []


def test_each_condition_fails_a_reading_on_its_own() -> None:
    short = su.figures(referenced(found(11), CLOCK))[lf.MAKER]
    assert su.failing(short, stopped=False) == [su.SHORT]
    below = su.figures(referenced(found(12), CLOCK, p_close_home=0.45))[lf.MAKER]
    assert su.failing(below, stopped=False) == [su.NOT_ABOVE]
    sample = su.figures(referenced(found(12), CLOCK))[lf.MAKER]
    assert su.failing(sample, stopped=True) == [su.STOPPED]
    # A fill at or after its proxy has no reference: under 90% referenced fails the floor.
    late = found(12).with_columns(
        fill_utc=pl.when(pl.col("game_id") % 5 == 0)
        .then(pl.col("start_utc") - timedelta(minutes=10))
        .otherwise("fill_utc")
    )
    uncovered = su.figures(referenced(late, CLOCK))[lf.TAKER]
    assert uncovered["coverage"] == pytest.approx(0.8, abs=0.01)
    assert su.failing(uncovered, stopped=False) == [su.UNCOVERED]


def test_taker_and_maker_fills_are_read_apart() -> None:
    # Only maker fills: the taker part has no sample, so it can't pass while the maker part does.
    figures = su.figures(referenced(found(12, kinds=(lf.MAKER,)), CLOCK))
    assert figures[lf.TAKER]["fills"] == 0 and figures[lf.TAKER]["clv_per_bid"] is None
    assert su.failing(figures[lf.TAKER], stopped=False) == [su.SHORT, su.UNCOVERED, su.NOT_ABOVE]
    assert su.failing(figures[lf.MAKER], stopped=False) == []


def test_the_reading_is_taken_as_of_its_clock(references: dict[str, float]) -> None:
    # Fills after the clock aren't read, and a stop alerted on the reading day itself isn't one
    # before it.
    fills = found(13)
    taken = su.take(fills, NO_ODDS, pl.DataFrame(), [READING], READING)
    assert taken["clock_utc"] == CLOCK.isoformat() and taken["stopped"] == []
    in_time = found(13).filter(pl.col("fill_utc") < CLOCK)
    assert taken[lf.TAKER]["fills"] == in_time.height // 2
    assert taken[lf.TAKER]["passes"] and taken[lf.MAKER]["passes"]
    stopped = su.take(fills, NO_ODDS, pl.DataFrame(), [date(2026, 12, 1)], READING)
    assert stopped["stopped"] == ["2026-12-01"]
    assert stopped[lf.TAKER]["failing"] == [su.STOPPED]


def test_the_private_block_has_no_verdict_before_the_clock_then_one_reading_kept(
    tmp_path: Path, references: dict[str, float]
) -> None:
    store = lb.LocalStore(tmp_path)
    fills = found(12)
    before = CLOCK - timedelta(minutes=1)
    early = su.private(store, fills, referenced(fills, before), NO_ODDS, pl.DataFrame(), [], before)
    assert early["reading_date"] == READING.isoformat() and not early["read"]
    assert "passes" not in json.dumps(early) and early["context"][lf.TAKER]["weeks"] == 12
    assert not store.keys("real/live/scale-up")
    # From the clock, the reading is taken once and kept; a later run with a worse close repeats
    # it, never reads again.
    taken = su.private(store, fills, referenced(fills, CLOCK), NO_ODDS, pl.DataFrame(), [], CLOCK)
    assert taken["read"] and taken[lf.TAKER]["passes"]
    assert store.keys("real/live/scale-up") == [su.reading_key(READING)]
    references["p_close_home"] = 0.40
    later = CLOCK + timedelta(days=8)
    again = su.private(
        store, found(14), referenced(found(14), later), NO_ODDS, pl.DataFrame(), [], later
    )
    assert again == taken


def test_without_a_fill_nothing_is_read() -> None:
    store = lb.LocalStore(Path("/nonexistent"))
    none = su.private(store, found(1).head(0), su.empty(), NO_ODDS, pl.DataFrame(), [], CLOCK)
    assert none == {"reading_date": None}
    assert su.count_line(none) == "real fills live: scale-up (ADR 0047) waits for a first fill"


def test_the_public_counts_carry_no_clv_and_no_verdict() -> None:
    fills = found(12)
    counts = su.public(referenced(fills, CLOCK), fills, CLOCK)
    assert counts["reading_date"] == READING.isoformat() and counts["due"]
    assert not counts["read"] and su.public(referenced(fills, CLOCK), fills, CLOCK, True)["read"]
    # From Wednesday 2026-11-18 to Sunday 2027-02-07: 82 days in 12 weeks.
    assert counts["samples"][lf.MAKER] == {
        "fills": 82,
        "weeks": 12,
        "started": 82,
        "valued": 82,
        "coverage": 1.0,
    }
    text = json.dumps(counts)
    assert "clv" not in text and "passes" not in text and "failing" not in text
    assert su.public(su.empty(), fills.head(0), CLOCK)["reading_date"] is None


def test_the_log_line_has_counts_only(tmp_path: Path, references: dict[str, float]) -> None:
    store = lb.LocalStore(tmp_path)
    fills = found(3)
    now = datetime(2026, 12, 7, 12, tzinfo=UTC)
    block = su.private(store, fills, referenced(fills, now), NO_ODDS, pl.DataFrame(), [], now)
    line = su.count_line(block)
    assert (
        line
        == "real fills live: scale-up (ADR 0047) reads on 2027-02-08; taker 3 weeks, maker 3 weeks"
    )


def test_the_reading_waits_for_every_order_before_its_clock_to_have_its_fills(
    tmp_path: Path, references: dict[str, float]
) -> None:
    # Codex on #351: an expired order not yet reconciled has fills the reading would leave out.
    from nhl_edge.live import placement as pm

    store = lb.LocalStore(tmp_path)
    fills = found(12)
    begins = datetime(2027, 2, 7, 23, tzinfo=UTC)
    order = {"game_date": "2027-02-07", "game_id": 2026020999, "start_utc": begins.isoformat()}
    lb.put_once(
        store, pm.order_key(pm.LIVE, date(2027, 2, 7), 2026020999, ""), json.dumps(order).encode()
    )
    assert su.unfinished(store, CLOCK) == 1
    waiting = su.private(store, fills, referenced(fills, CLOCK), NO_ODDS, pl.DataFrame(), [], CLOCK)
    assert not waiting["read"] and waiting["waiting"] == 1 and not su.sealed(store, fills)
    assert su.count_line(waiting).startswith(
        "real fills live: scale-up (ADR 0047) due, waiting for 1 orders' fills, on 2027-02-08"
    )
    # Once it has its final record, the reading is taken, as of the clock still.
    final = {"status": "not sent", "reason": "the send failed", "read_utc": CLOCK.isoformat()}
    lb.put_once(
        store,
        pm.order_key(pm.LIVE, date(2027, 2, 7), 2026020999, "final"),
        json.dumps(final).encode(),
    )
    later = CLOCK + timedelta(days=1)
    taken = su.private(store, fills, referenced(fills, later), NO_ODDS, pl.DataFrame(), [], later)
    assert taken["read"] and taken["clock_utc"] == CLOCK.isoformat() and su.sealed(store, fills)
    # An order of a game after the clock never holds it.
    assert su.unfinished(store, CLOCK) == 0
