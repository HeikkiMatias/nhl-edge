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
