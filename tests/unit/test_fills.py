"""#318: the real Kalshi fills, valued against the close for taker and maker apart, only from
the fills observed before their game's Pinnacle proxy."""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import fill_fixtures as ff
import polars as pl
import predict_fixtures as pf
import pytest
from typer.testing import CliRunner

from nhl_edge.cli import app
from nhl_edge.live import bundle as lb
from nhl_edge.live import fills as lf
from nhl_edge.live import placement as pm

TAKER = ff.fill(ff.PLACED, "1.00", "0.5000", "0.0175", True)
MAKER = ff.fill(ff.PROXY - timedelta(hours=2), "2.00", "0.4800", "0.0044", False)


def rows_of(
    tmp_path: Path,
    fills: list[dict[str, Any]],
    odds: pl.DataFrame | None = None,
    now: datetime = ff.AFTER,
    team: str = "WSH",
) -> pl.DataFrame:
    store = lb.LocalStore(tmp_path)
    ff.record(store, fills, team=team)
    refs = lf.references(ff.odds() if odds is None else odds, now)
    return lf.referenced(lf.fills(store, pm.DEMO), refs, ff.TEAMS, now)


def test_taker_and_maker_parts_are_valued_apart_at_their_cost_fees_included(
    tmp_path: Path,
) -> None:
    bids = lf.bids(rows_of(tmp_path, [TAKER, MAKER]))
    taker = bids.filter(pl.col("kind") == lf.TAKER).row(0, named=True)
    maker = bids.filter(pl.col("kind") == lf.MAKER).row(0, named=True)
    assert taker["clv"] == pytest.approx(ff.P_HOME / 0.5175 - 1)
    assert maker["clv"] == pytest.approx(ff.P_HOME * 2 / 0.9644 - 1)
    # The taker filled at placement; the maker rested almost four hours.
    assert taker["band"] == "under 1 minute"
    assert maker["band"] == "1 to 4 hours"


def test_an_adr_0045_orders_two_parts_are_its_taker_and_maker_bids(tmp_path: Path) -> None:
    # #321: the taker part's fills and the maker part's, each tagged by Kalshi's is_taker, with
    # the delay from the taker part's send, the first to go.
    store = lb.LocalStore(tmp_path)
    ff.two_parts(store, [TAKER], [MAKER])
    refs = lf.references(ff.odds(), ff.AFTER)
    bids = lf.bids(lf.referenced(lf.fills(store, pm.DEMO), refs, ff.TEAMS, ff.AFTER))
    assert sorted(bids["kind"].to_list()) == [lf.MAKER, lf.TAKER]
    maker = bids.filter(pl.col("kind") == lf.MAKER).row(0, named=True)
    assert maker["clv"] == pytest.approx(ff.P_HOME * 2 / 0.9644 - 1)
    assert maker["band"] == "1 to 4 hours"
    final = json.loads(store.get(pm.order_key(pm.DEMO, pf.DAY, ff.WSH, "final")))
    assert final["parts"]["taker"]["contracts"] == "1.00"
    assert final["parts"]["maker"]["contracts"] == "2.00"
    assert final["contracts"] == "3.00" and final["filled_contracts"] == "3.00"


def test_the_delay_runs_from_the_send_never_from_a_later_adoption(tmp_path: Path) -> None:
    # An adopted order's placed time is when a later run found it on Kalshi, hours after the
    # send and the fill: the delay reads Kalshi's creation time, or the record written before the
    # send, instead (Codex on #324).
    found_later = ff.PROXY + timedelta(minutes=50)
    for name, created, band in (
        ("written", None, "1 to 4 hours"),
        ("created", ff.PLACED - timedelta(hours=1), "4 hours or more"),
    ):
        store = lb.LocalStore(tmp_path / name)
        ff.record(store, [MAKER], placed_at=found_later, adopted=True, created=created)
        refs = lf.references(ff.odds(), ff.AFTER)
        rows = lf.referenced(lf.fills(store, pm.DEMO), refs, ff.TEAMS, ff.AFTER)
        assert lf.bids(rows)["band"].to_list() == [band]


def test_the_away_team_takes_the_away_side_of_the_close(tmp_path: Path) -> None:
    bid = lf.bids(rows_of(tmp_path, [TAKER], team="PIT")).row(0, named=True)
    assert bid["p_close"] == pytest.approx(1 - ff.P_HOME)


def test_a_part_keeps_only_its_fills_before_the_proxy(tmp_path: Path) -> None:
    late = ff.fill(ff.PROXY + timedelta(minutes=20), "3.00", "0.4500", "0.0050", False)
    rows = rows_of(tmp_path, [MAKER, late])
    assert sorted(rows["reference"]) == [lf.AFTER_REFERENCE, lf.VALUED]
    bid = lf.bids(rows).row(0, named=True)
    assert bid["contracts"] == 2.0
    assert bid["cost_usd"] == pytest.approx(0.9644)


def test_a_game_without_a_proxy_has_no_reference(tmp_path: Path) -> None:
    rows = rows_of(tmp_path, [TAKER, MAKER], odds=ff.odds(None))
    assert set(rows["reference"]) == {lf.NO_REFERENCE}
    assert lf.bids(rows).is_empty()


def test_a_fill_whose_team_is_not_the_games_has_no_reference(tmp_path: Path) -> None:
    rows = rows_of(tmp_path, [TAKER], team="NYR")
    assert rows["reference"].to_list() == [lf.NO_REFERENCE]
    assert lf.bids(rows).is_empty()


def test_a_game_not_yet_started_waits(tmp_path: Path) -> None:
    rows = rows_of(tmp_path, [TAKER], now=pf.START - timedelta(minutes=1))
    assert rows["reference"].to_list() == [lf.PENDING]


def test_a_fill_without_is_taker_counts_apart(tmp_path: Path) -> None:
    unknown = ff.fill(ff.PLACED, "1.00", "0.5000", "0.0175", None)
    rows = rows_of(tmp_path, [unknown])
    assert rows["kind"].to_list() == [lf.UNKNOWN]
    assert lf.UNKNOWN in lf.summary(rows)
    assert lf.UNKNOWN not in lf.summary(rows_of(tmp_path / "known", [TAKER]))


def synthetic(weeks: int) -> pl.DataFrame:
    """Referenced rows of one valued maker fill a day for weeks weeks, at a fixed CLV."""
    days = [date(2026, 11, 2) + timedelta(days=d) for d in range(7 * weeks)]
    placed = [datetime(d.year, d.month, d.day, 17, tzinfo=ff.PROXY.tzinfo) for d in days]
    return pl.DataFrame(
        {
            "game_date": days,
            "season": [20262027] * len(days),
            "game_id": [2026020100 + i for i in range(len(days))],
            "team": ["WSH"] * len(days),
            "kind": [lf.MAKER] * len(days),
            "reference": [lf.VALUED] * len(days),
            "p_close": [0.55] * len(days),
            "contracts": [1.0] * len(days),
            "cost_usd": [0.5] * len(days),
            "placed_utc": placed,
            "fill_utc": [p + timedelta(hours=2) for p in placed],
        }
    )


def test_the_figures_are_counts_only_under_four_weeks() -> None:
    few = lf.summary(synthetic(3))[lf.MAKER]
    assert few["bids"] == 21
    assert set(few["clv_per_bid"]) == {"games", "weeks"}
    enough = lf.summary(synthetic(4))[lf.MAKER]
    assert enough["clv_per_bid"]["mean"] == pytest.approx(0.1)
    # A constant CLV: the interval is the point itself.
    assert enough["clv_per_bid"]["low"] == pytest.approx(0.1)
    assert enough["clv_per_bid"]["high"] == pytest.approx(0.1)
    assert enough["by_delay"]["1 to 4 hours"]["bids"] == 28
    assert enough["by_delay"]["under 1 minute"] == {"bids": 0, "clv_per_bid": None}


def test_the_log_line_has_counts_only(tmp_path: Path) -> None:
    line = lf.count_line(pm.DEMO, rows_of(tmp_path, [TAKER, MAKER]))
    assert line.startswith("real fills demo: 1 orders filled; taker 1 (1 valued")
    assert "." not in line


def test_nhl_real_fills_writes_the_figures_privately_beside_the_orders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge import cli
    from nhl_edge.live import real_ledger as rl

    store = lb.LocalStore(tmp_path / "record")
    ff.record(store, [TAKER, MAKER])
    alerts = ff.TEAMS.with_columns(game_date=pl.lit(pf.DAY))

    class FakeLake:
        def read(self, table: str) -> pl.DataFrame:
            assert table == "odds_snapshots"
            return ff.odds()

    class Later(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return ff.AFTER

    monkeypatch.setattr(cli, "_real_record", lambda r2: (FakeLake(), store))
    monkeypatch.setattr(rl, "alerts", lambda found: alerts)
    monkeypatch.setattr(cli, "datetime", Later)
    monkeypatch.setattr("nhl_edge.backtest.reports.version", lambda name, now: "real-fills-x")
    result = CliRunner().invoke(app, ["real", "fills"])
    assert result.exit_code == 0, result.output
    assert "real fills demo: 1 orders filled; taker 1 (1 valued" in result.output
    assert "real fills live: no fills" in result.output
    (key,) = store.keys("real/demo/fill-reports")
    assert key == lf.report_key(pm.DEMO, ff.AFTER)
    figures = json.loads(store.get(key))
    assert figures["env"] == pm.DEMO and figures["code_version"] == "real-fills-x"
    assert figures[lf.TAKER]["bids"] == 1 and figures[lf.MAKER]["bids"] == 1
    assert not store.keys("real/live")


def test_nhl_real_fills_adds_the_scale_up_block_to_the_live_figures_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR 0047 (#322): before the reading's clock, the figures as context and no verdict, kept
    # privately; the log has the reading date and the weeks of fills only.
    from nhl_edge import cli
    from nhl_edge.live import real_ledger as rl
    from nhl_edge.live import scale_up as su

    store = lb.LocalStore(tmp_path / "record")
    ff.record(store, [TAKER, MAKER], env=pm.LIVE)
    alerts = ff.TEAMS.with_columns(game_date=pl.lit(pf.DAY))

    class FakeLake:
        def read(self, table: str) -> pl.DataFrame:
            return ff.odds()

    class Later(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return ff.AFTER

    monkeypatch.setattr(cli, "_real_record", lambda r2: (FakeLake(), store))
    monkeypatch.setattr(rl, "alerts", lambda found: alerts)
    monkeypatch.setattr(cli, "datetime", Later)
    monkeypatch.setattr("nhl_edge.backtest.reports.version", lambda name, now: "real-fills-x")
    result = CliRunner().invoke(app, ["real", "fills"])
    assert result.exit_code == 0, result.output
    day = pf.DAY - timedelta(days=pf.DAY.weekday()) + timedelta(weeks=12)
    assert (
        f"real fills live: scale-up (ADR 0047) reads on {day}; taker 1 weeks, maker 1 weeks"
        in result.output
    )
    (key,) = store.keys("real/live/fill-reports")
    block = json.loads(store.get(key))["scale_up"]
    assert block["reading_date"] == day.isoformat() and not block["read"]
    assert block["context"][lf.TAKER]["valued"] == 1 and "passes" not in json.dumps(block)
    assert not store.keys("real/live/scale-up")
    assert su.reading_key(day) not in store.keys("real/live")


def test_the_weekly_reports_scale_up_counts_come_from_the_live_fills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # nhl live report --r2 reads only counts from the live exchange's fills (#322, #280).
    from nhl_edge import cli
    from nhl_edge.live import real_ledger as rl

    store = lb.LocalStore(tmp_path / "record")
    assert cli._scale_up_counts(store, ff.odds(), ff.AFTER)["reading_date"] is None
    ff.record(store, [TAKER, MAKER], env=pm.LIVE)
    # The demo exchange's mock fills never count.
    ff.record(store, [TAKER, TAKER], env=pm.DEMO)
    monkeypatch.setattr(rl, "alerts", lambda found: ff.TEAMS.with_columns(game_date=pl.lit(pf.DAY)))
    counts = cli._scale_up_counts(store, ff.odds(), ff.AFTER)
    assert counts["samples"][lf.TAKER] == {
        "fills": 1,
        "weeks": 1,
        "started": 1,
        "valued": 1,
        "coverage": 1.0,
    }
    assert not counts["read"] and "clv" not in json.dumps(counts)
