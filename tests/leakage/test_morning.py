"""Point-in-time rules for the morning view (#357, ADR 0052, hard rule 1): everything it reads was
known before the morning snapshot. Quotes after the run, the midday's and the close included,
change nothing; the starters and the models are read at the morning snapshot, and only the polls
observed before it are saved; a feature build that finished after it is refused. B2's, B3's and
u's own rules at a prediction time are tested in test_b2.py, test_b3.py and test_uncertainty.py,
the starters' in test_confirmed_goalies.py, and the decision's in test_predict.py."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl
import predict_fixtures as pf

from nhl_edge import cli
from nhl_edge.live import goalies as live_goalies
from nhl_edge.live import morning as lm
from nhl_edge.live import predict as lp

RUN = datetime(2026, 10, 7, 11, 8, tzinfo=UTC)


def made(quotes: pl.DataFrame) -> pl.DataFrame:
    inputs = pf.day(decision_utc=RUN, published_utc=RUN + timedelta(minutes=3), quotes=quotes)
    return lm.rows(lm.predict(inputs), inputs)


def test_quotes_after_the_run_change_nothing() -> None:
    before = made(pf.day_quotes())
    later = RUN + timedelta(seconds=1)
    close = pf.START - timedelta(minutes=5)
    quotes = pl.concat(
        [
            pf.day_quotes(),
            # A morning-labelled snapshot after the run, still in the window, and the close.
            pf.quotes(pf.quote(later, 2026020053, "pinnacle", 3.0, 1.4, slot="morning")),
            pf.quotes(pf.quote(close, 2026020054, "pinnacle", 1.2, 4.5, slot="pre7")).with_columns(
                is_closing_proxy=pl.lit(True)
            ),
        ]
    )
    assert made(quotes).equals(before)
    # Without the midday snapshot, which came after the run, the rows are the same too.
    assert made(pf.day_quotes().filter(pl.col("slot") == "morning")).equals(before)


def test_the_morning_snapshot_precedes_the_run() -> None:
    quotes = pf.day_quotes()
    # Run at the snapshot's own instant, the snapshot isn't yet known.
    assert lm.morning_snapshot(quotes, pf.MORNING) is None
    assert lm.morning_snapshot(quotes, pf.MORNING + timedelta(microseconds=1)) == pf.MORNING
    assert lm.input_cutoff(quotes, RUN) == pf.MORNING


def test_a_feature_build_finished_after_the_morning_snapshot_is_refused() -> None:
    cutoff = lm.input_cutoff(pf.day_quotes(), RUN)
    slate = pf.slate()
    for finished, refused in ((cutoff - timedelta(seconds=1), False), (cutoff, True)):
        record = pl.DataFrame(
            {
                "build_id": ["live-features-20261007-abc1234"],
                "finished_utc": [finished],
                "slate_raw_key": [slate["raw_key"][0]],
                "table": ["slate"],
                "artifact_version": ["slate-20261007-abc1234"],
            }
        )
        problems = lp.build_problems(record, slate, cutoff, {})
        assert any("not before the decision" in p for p in problems) == refused


@dataclass
class Lake:
    def read(self, table: str, seasons: Any = None) -> pl.DataFrame:
        return pl.DataFrame()


@dataclass
class Spy:
    calls: dict[str, Any] = field(default_factory=dict)

    def with_starters(self, tables: Any, b3_tables: Any, u_tables: Any, *args: Any) -> Any:
        # slate, polls, players, cutoff, likely_share
        self.calls["starters"] = args[3]
        starters = pl.DataFrame({"game_id": [2026020053], "team": ["WSH"], "source": ["NHL"]})
        return tables, b3_tables, u_tables, starters

    def models(self, *args: Any) -> lp.Models:
        # tables, b3_tables, u_tables, slate, moments, start
        self.calls["models"] = args[4:]
        return pf.fitted()


def test_the_starters_and_models_are_read_at_the_morning_snapshot(monkeypatch: Any) -> None:
    cutoff = lm.input_cutoff(pf.day_quotes(), RUN)
    seen = pl.DataFrame(
        {"observed_utc": [cutoff - timedelta(minutes=2), cutoff, cutoff + timedelta(minutes=1)]}
    )
    spy = Spy()
    monkeypatch.setattr(live_goalies, "day_polls", lambda store, day: (seen, seen))
    monkeypatch.setattr(live_goalies, "with_starters", spy.with_starters)
    monkeypatch.setattr(lp, "models", spy.models)
    start = pf.live().fold_start
    lake: Any = Lake()
    # No raw store is read: the polls come from day_polls, and nothing is restored without --r2.
    store: Any = None
    _, _, calls = cli._day_models(
        lake, store, pf.slate(), pl.DataFrame(), cutoff, start, 0.8, False, show_starters=False
    )
    assert spy.calls["starters"] == cutoff
    moments, fold = spy.calls["models"]
    assert set(moments["prediction_utc"]) == {cutoff} and fold == start
    # Only the polls observed before the morning snapshot are saved beside the rows.
    for name in ("pregame_goalies", "dailyfaceoff_goalies"):
        assert calls[name]["observed_utc"].to_list() == [cutoff - timedelta(minutes=2)]


@dataclass
class DayLake:
    """The lake as nhl live morning reads it for the fixture's day: its slate, its build's
    record, and no other rows."""

    finished: datetime

    def read(self, table: str, seasons: Any = None) -> pl.DataFrame:
        if table == "slate":
            return pf.slate()
        if table == "feature_builds":
            return pl.DataFrame(
                {
                    "game_date": [pf.DAY],
                    "table": ["slate"],
                    "artifact_version": ["live-features-20261007-abc1234"],
                    "build_id": ["live-features-20261007-abc1234"],
                    "slate_games": [3],
                    "slate_raw_key": [pf.slate()["raw_key"][0]],
                    "finished_utc": [self.finished],
                }
            )
        return pl.DataFrame(schema={"game_date": pl.Date})


def test_the_command_reads_everything_at_the_morning_snapshot(
    tmp_path: Any, monkeypatch: Any
) -> None:
    import json

    from typer.testing import CliRunner

    from nhl_edge import settings
    from nhl_edge.lake.raw import RawStore
    from nhl_edge.lake.tables import Lake

    seen: dict[str, Any] = {}

    def problems(record: Any, slate: Any, cutoff: datetime, rows: Any, **_: Any) -> list[str]:
        seen["build"] = cutoff
        return []

    def models(lake: Any, store: Any, slate: Any, games: Any, cutoff: datetime, *_: Any, **__: Any):
        seen["models"] = (lake, cutoff)
        return pf.fitted(), None, {}

    monkeypatch.setattr(settings, "load_env", lambda: None)
    monkeypatch.setattr(Lake, "from_env", classmethod(lambda cls, mirror: DayLake(pf.MORNING)))
    monkeypatch.setattr(RawStore, "from_env", classmethod(lambda cls, mirror, flag: None))
    monkeypatch.setattr(lp, "day_quotes", lambda store, day: (pf.day_quotes(), []))
    monkeypatch.setattr(lp, "raw_responses", lambda store, day, by=None: {})
    monkeypatch.setattr(lp, "build_problems", problems)
    monkeypatch.setattr(cli, "_day_models", models)
    at = RUN.strftime("%Y-%m-%dT%H:%M:%S%z")
    command = ["live", "morning", "--date", "2026-10-07", "--dry-run", "--at", at]
    result = CliRunner().invoke(cli.app, [*command, "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    # The build's check, the models and the bundle all read at the morning snapshot.
    assert seen["build"] == pf.MORNING
    lake, cutoff = seen["models"]
    assert cutoff == pf.MORNING and isinstance(lake, lm.AtBuild)
    manifest = json.loads((tmp_path / "bundles/morning/2026-10-07/manifest.json").read_text())
    assert manifest["input_cutoff"] == pf.MORNING.isoformat()
    rows = pl.read_parquet(tmp_path / "morning/live/2026-10-07.parquet")
    assert set(rows["status"]) == {lm.PREDICTED}
    assert set(rows["morning_snapshot_utc"]) == {pf.MORNING}
