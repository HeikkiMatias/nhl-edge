"""The morning view (#357, ADR 0052): the decision's models at the morning snapshot, with no bet.
Its window and statuses, its probabilities from the decision's own code at the morning price,
its rows written once, and a stored day replayed from its bundle."""

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import predict_fixtures as pf
import pytest
from typer.testing import CliRunner

from nhl_edge.cli import app
from nhl_edge.lake.schemas import Slate, dtypes
from nhl_edge.live import bundle as lb
from nhl_edge.live import morning as lm
from nhl_edge.live import predict as lp
from nhl_edge.market.devig import fair_probabilities

RUN = datetime(2026, 10, 7, 11, 8, tzinfo=UTC)  # 07:08 ET, after the 07:06 snapshot
PUBLISHED = RUN + timedelta(minutes=3)
RECORD = pl.DataFrame(
    {"build_id": ["live-features-20261007-abc1234"], "slate_raw_key": ["k"]},
)


def day(**changes: Any) -> lp.Day:
    return pf.day(**({"decision_utc": RUN, "published_utc": PUBLISHED} | changes))


def made(inputs: lp.Day) -> pl.DataFrame:
    return lm.rows(lm.predict(inputs), inputs)


def test_the_window_runs_from_07_00_to_07_35_et() -> None:
    eastern = [(10, 59, False), (11, 0, True), (11, 35, True), (11, 36, False)]
    for hour, minute, inside in eastern:
        assert lm.in_window(datetime(2026, 10, 7, hour, minute, tzinfo=UTC)) == inside
    assert lm.after_window(datetime(2026, 10, 7, 11, 36, tzinfo=UTC))
    assert not lm.after_window(datetime(2026, 10, 7, 10, 59, tzinfo=UTC))


def test_the_morning_rows_are_the_decisions_models_at_the_morning_price() -> None:
    inputs = day()
    rows = made(inputs)
    assert rows["status"].to_list() == [lm.PREDICTED] * 3
    assert set(rows["morning_snapshot_utc"]) == {pf.MORNING}
    assert set(rows["morning_version"]) == {lm.VERSION}
    # B0: Pinnacle's morning pair de-vigged (hard rule 3).
    pairs = rows.select("home_price", "away_price").to_numpy()
    np.testing.assert_allclose(rows["p_b0"].to_numpy(), fair_probabilities(pairs)[:, 0])
    # Every probability is the decision's own code on the morning's prices.
    priced = rows.select("game_id", "event_id", "home_price", "away_price", "last_update_utc")
    expected = lp.predictions(priced, inputs.fitted, inputs.live)  # type: ignore[arg-type]
    for column in ("p_b1", "p_b2", "p_b3", "u", "u_sd", "p_blend", "p_blend_b2", "p_blend_market"):
        np.testing.assert_allclose(rows[column].to_numpy(), expected[column].to_numpy())


def test_a_game_without_a_fresh_morning_price_or_its_models_has_no_prediction() -> None:
    old = pf.MORNING - lp.MAX_QUOTE_AGE - timedelta(seconds=1)
    quotes = pf.quotes(
        pf.quote(pf.MORNING, 2026020053, "pinnacle", 2.1, 1.8, slot="morning"),
        pf.quote(pf.MORNING, 2026020054, "pinnacle", 1.9, 2.0, updated=old, slot="morning"),
    )
    rows = made(day(quotes=quotes)).sort("game_id")
    assert rows["status"].to_list() == [lm.PREDICTED, lm.STALE, lm.NO_PRICE]
    assert rows.filter(pl.col("status") != lm.PREDICTED)["p_blend"].is_null().all()
    # Without the day's models, every priced game is a missing input.
    unfitted = made(day(quotes=quotes, fitted=None)).sort("game_id")
    assert unfitted["status"].to_list() == [lm.MISSING, lm.STALE, lm.NO_PRICE]


@pytest.mark.parametrize(
    ("run", "status"),
    [
        (datetime(2026, 10, 7, 10, 50, tzinfo=UTC), lm.EARLY),
        (datetime(2026, 10, 7, 11, 40, tzinfo=UTC), lm.LATE),
    ],
)
def test_a_run_outside_the_window_writes_the_day_as_skipped(run: datetime, status: str) -> None:
    rows = made(day(decision_utc=run, published_utc=run + timedelta(minutes=2)))
    assert set(rows["status"]) == {status}
    assert rows["p_b3"].is_null().all()


def test_a_day_without_a_morning_snapshot_in_the_window_is_skipped() -> None:
    midday_only = pf.day_quotes().filter(pl.col("slot") == "midday")
    rows = made(day(quotes=midday_only))
    assert set(rows["status"]) == {lm.NO_SNAPSHOT}


def test_a_game_that_starts_before_the_publication_has_no_prediction() -> None:
    early = {**pf.GAMES, 2026020053: ("WSH", "PIT", PUBLISHED - timedelta(minutes=1))}
    rows = [
        {
            **row,
            "start_utc": early[row["game_id"]][2],
        }
        for row in pf.slate().iter_rows(named=True)
    ]
    slate = Slate.validate(pl.DataFrame(rows, schema=dtypes(Slate)))
    out = made(day(slate=slate)).sort("game_id")
    assert out["status"].to_list()[0] == lm.STARTS_BEFORE_PUBLISHED


def test_the_rows_are_written_once_and_rebuilt(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    inputs = day()
    clock = iter([RUN + timedelta(minutes=k) for k in range(1, 10)])
    written, key = lm.write_published(store, inputs, lambda: next(clock))
    assert key == "morning/live/2026-10-07.parquet"
    assert written["published_utc"].n_unique() == 1
    with pytest.raises(FileExistsError):
        lm.write_published(store, inputs, lambda: next(clock))
    assert lm.stored_days(store) == [pf.DAY]
    assert lm.sync(store).equals(written)


def test_a_stored_day_replays_from_its_bundle_and_a_change_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    inputs = day()
    written, key = lm.write_published(store, inputs, lambda: PUBLISHED)
    cutoff = lm.input_cutoff(inputs.quotes, RUN)
    identity = lm.identity(inputs, cutoff, PUBLISHED, "blend-live.json", "0" * 64)
    rows = lb.inputs(inputs.slate, RECORD, inputs.quotes, None)
    lb.write_once(store, pf.DAY, rows, {}, identity, {}, lm.BUNDLES)
    # The bundle holds no fits here: the replay's models are the day's own.
    monkeypatch.setattr(lb, "models", lambda _: inputs.fitted)
    assert lm.check(store, pf.DAY, inputs.live) == []
    # The decision's bundle is elsewhere, and untouched.
    assert lb.unfinished(store, [pf.DAY]) == [pf.DAY]
    moved = written.with_columns(p_blend=pl.col("p_blend") * 0.99)
    (tmp_path / key).write_bytes(lb.parquet(moved))
    assert lm.check(store, pf.DAY, inputs.live) == [
        "2026-10-07: p_blend differs for " + str(sorted(pf.GAMES))
    ]
    other = replace(inputs.live, version="blend-live-20261008-def5678")
    assert "not blend-live-20261008-def5678" in lm.check(store, pf.DAY, other)[0]


def plain(text: str) -> str:
    unstyled = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", unstyled).split())


def test_the_command_runs_only_at_todays_morning_slot_and_refuses_a_held_dry_run(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    midday = runner.invoke(app, ["live", "morning", "--r2", "--cron", "45 16 * * *"])
    assert midday.exit_code == 0 and "no morning view" in midday.output
    real = runner.invoke(app, ["live", "morning"])
    assert real.exit_code == 2 and "pass --r2" in plain(real.output)
    (tmp_path / lb.prefix(pf.DAY, lm.BUNDLES)).mkdir(parents=True)
    at = RUN.strftime("%Y-%m-%dT%H:%M:%S%z")
    command = ["live", "morning", "--date", "2026-10-07", "--dry-run", "--at", at]
    held = runner.invoke(app, [*command, "--out", str(tmp_path)])
    assert held.exit_code == 2 and "pass a fresh --out" in plain(held.output), held.output


@dataclass
class Rows:
    frame: pl.DataFrame

    def read(self, table: str, seasons: Any = None) -> pl.DataFrame:
        return self.frame


def test_the_days_own_feature_rows_count_as_observed_when_their_build_had_them() -> None:
    # History's as-of time of 10:00 ET stamps the day's rows (live/targets.py); the build that
    # wrote them finished at 05:32 ET, before the night's results were public by convention.
    stamp = datetime(2026, 10, 7, 14, tzinfo=UTC)
    rows = pl.DataFrame(
        {
            "game_id": [1, 2, 3],
            "game_date": [pf.DAY, pf.DAY, pf.DAY - timedelta(days=1)],
            "artifact_version": ["team-strength-a", "team-strength-b", "team-strength-a"],
            "observed_utc": [stamp, stamp, stamp - timedelta(days=1)],
        }
    )
    record = pl.DataFrame(
        {
            "table": ["team_strength"],
            "artifact_version": ["team-strength-a"],
            "finished_utc": [datetime(2026, 10, 7, 9, 32, tzinfo=UTC)],
        }
    )
    at_build = lm.AtBuild(Rows(rows), record, pf.DAY)
    public = datetime(2026, 10, 7, 10, tzinfo=UTC)
    assert at_build.known() == public
    read = at_build.read("team_strength")["observed_utc"].to_list()
    # Only the day's rows of its own build move, and to the later of the finish and the results.
    assert read == [public, stamp, stamp - timedelta(days=1)]
    # A table the build didn't write is the lake's own.
    assert at_build.read("goalie_starts").equals(rows)


def test_a_bundle_saved_with_another_cutoff_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    inputs = day()
    lm.write_published(store, inputs, lambda: PUBLISHED)
    # The run's own instant, not the morning snapshot, as the models' cutoff.
    identity = lm.identity(inputs, RUN, PUBLISHED, "blend-live.json", "0" * 64)
    rows = lb.inputs(inputs.slate, RECORD, inputs.quotes, None)
    lb.write_once(store, pf.DAY, rows, {}, identity, {}, lm.BUNDLES)
    monkeypatch.setattr(lb, "models", lambda _: inputs.fitted)
    (problem,) = lm.check(store, pf.DAY, inputs.live)
    assert "is not the morning snapshot" in problem
