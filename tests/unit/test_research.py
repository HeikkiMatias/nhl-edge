"""Research run bundles (#186): a backtest run's per-game rows, kept once, reproduce its report's
estimates, and two kept runs can be paired game by game."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest

from nhl_edge.backtest import b3_report, blend, reports, research
from nhl_edge.backtest.walk_forward import PREDICTION_SCHEMA
from nhl_edge.live import bundle as lb
from nhl_edge.market.recalibration import Recalibration

NOW = datetime(2026, 10, 21, 18, 5, 9, tzinfo=UTC)
SEASONS = [20182019, 20212022]
# The blend's training fold, kept beside the tested seasons and out of every estimate.
TRAINING = 20192020
GAMES = 40
# Each model's rows: (model, method). B0 under two methods, so it is paired with the default.
MODELS = [
    ("B0", "multiplicative"),
    ("B0", "power"),
    ("B1", "multiplicative"),
    ("B2", "none"),
    ("B3", "none"),
    (blend.BLEND, "none"),
    (blend.BLEND_B2, "none"),
    (blend.BLEND_MARKET, "none"),
]


def predictions(seed: int = 3, models: list[tuple[str, str]] = MODELS) -> pl.DataFrame:
    """Every model's rows for E1 on GAMES games a season over several weeks, with results."""
    rng = np.random.default_rng(seed)
    rows = []
    for season in [*SEASONS, TRAINING]:
        first = date(season // 10000, 10, 3)
        for k in range(GAMES):
            game_id = season // 10000 * 1_000_000 + 20_000 + k
            day = first + timedelta(days=3 * k)
            home_win = int(rng.random() < 0.55)
            moment = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=14)
            for model, method in models:
                p = float(np.clip(0.47 + 0.06 * home_win + rng.normal(0, 0.08), 0.05, 0.95))
                loss = -np.log(p if home_win else 1 - p)
                rows.append(
                    ("E1", model, method, season, game_id, day, moment, None, p, home_win, loss)
                )
    return pl.DataFrame(rows, schema=PREDICTION_SCHEMA, orient="row")


def b1_fits() -> dict[str, dict[int, Recalibration]]:
    cutoff = datetime(2018, 9, 1, tzinfo=UTC)
    return {"E1": {s: Recalibration(0.01, 1.02, 500, cutoff) for s in [*SEASONS, TRAINING]}}


def coverage() -> dict[str, dict[int, dict[str, int]]]:
    return {"E1": {s: {"scored": GAMES} for s in [*SEASONS, TRAINING]}}


def flags(rows: pl.DataFrame) -> pl.DataFrame:
    """Gate 2's subsets: every third game traded, every fourth injured."""
    ids = rows["game_id"].unique().sort()
    k = pl.Series(range(ids.len()))
    frame = pl.DataFrame({"game_id": ids}).with_columns(
        trade=(k % 3 == 0), injury=(k % 4 == 0), lineup_change=pl.lit(False)
    )
    return frame.with_columns(any=pl.col("trade") | pl.col("injury"))


def development_report(rows: pl.DataFrame) -> dict[str, Any]:
    """The development report as the CLI builds it: the summary, then B3's and the blend's
    sections from their add-ons' own code."""
    tested = rows.filter(pl.col("season").is_in(SEASONS))
    report = reports.summary(
        tested,
        {"E1": {s: c for s, c in coverage()["E1"].items() if s in SEASONS}},
        {"E1": {s: f for s, f in b1_fits()["E1"].items() if s in SEASONS}},
        SEASONS,
        "backtest-20261021-abc1234",
        NOW,
    )
    models = report["experiments"]["E1"]["models"]
    models["B3"]["paired_against_B2"] = b3_report._estimates(
        b3_report.against(tested), "difference"
    )
    models["B3"]["gate_2_subsets"] = b3_report.subsets(tested, flags(rows))
    models["B3"]["lineup_quality"] = {"a section": "the bundle doesn't recompute"}
    for reference in (blend.BLEND_B2, blend.BLEND_MARKET):
        models[blend.BLEND][f"paired_against_{reference}"] = blend._estimates(
            blend.paired(tested, blend.BLEND, reference), "difference"
        )
    return report


def development_files(rows: pl.DataFrame, report: dict[str, Any]) -> dict[str, bytes]:
    return research.files(
        rows,
        SEASONS,
        coverage(),
        report,
        {"games": "a" * 64, "sbr_odds": "b" * 64},
        b1_fits=b1_fits(),
        flags=flags(rows),
    )


IDENTITY = {"mode": research.DEVELOPMENT, "seasons": SEASONS}


def test_a_kept_run_reproduces_its_report_from_its_rows_alone(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    rows = predictions()
    run = research.run_id("backtest-20261021-abc1234", NOW)
    assert run == "backtest-20261021-abc1234-180509"
    problems = research.keep(
        store, run, development_files(rows, development_report(rows)), IDENTITY
    )
    assert problems == []
    kept = research.read(store, run)
    assert research.runs(store) == [run]
    assert kept.manifest["run"] == run and kept.manifest["mode"] == research.DEVELOPMENT
    # Every model's rows are kept, the blend's training fold marked apart from the tests.
    roles = kept.predictions.group_by("role").len().sort("role")
    assert roles.rows() == [
        (research.BLEND_TRAINING, GAMES * len(MODELS)),
        (research.TEST, 2 * GAMES * len(MODELS)),
    ]
    assert research.reproduce(kept) == []


def test_a_report_the_rows_dont_give_is_never_sealed(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    rows = predictions()
    report = development_report(rows)
    # One game's B3 loss in the kept rows differs from the one the report was computed on.
    changed = rows.with_columns(
        log_loss=pl.when((pl.col("model") == "B3") & (pl.col("game_id") == 2018020003))
        .then(pl.col("log_loss") + 0.5)
        .otherwise(pl.col("log_loss"))
    )
    run = "backtest-20261021-abc1234-180509"
    problems = research.keep(store, run, development_files(changed, report), IDENTITY)
    assert any("/experiments/E1/models/B3/log_loss" in p for p in problems)
    assert any("paired_against_B2" in p for p in problems)
    # Without its manifest the bundle never reads as complete, nor is it listed.
    with pytest.raises(ValueError, match="no manifest"):
        research.read(store, run)
    assert research.runs(store) == []


def test_a_second_run_leaves_the_first_intact(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    rows = predictions()
    first = "backtest-20261021-abc1234-180509"
    assert (
        research.keep(store, first, development_files(rows, development_report(rows)), IDENTITY)
        == []
    )
    before = {key: store.get(key) for key in store.keys(research.where(first))}
    other = predictions(seed=4)
    second = "backtest-20261021-abc1234-181200"
    assert (
        research.keep(store, second, development_files(other, development_report(other)), IDENTITY)
        == []
    )
    assert {key: store.get(key) for key in store.keys(research.where(first))} == before
    assert research.runs(store) == [first, second]
    # The same run id can't be written with other bytes.
    with pytest.raises(lb.Conflict):
        research.write_files(store, first, development_files(other, development_report(other)))


def test_an_altered_file_is_refused(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    rows = predictions()
    run = "backtest-20261021-abc1234-180509"
    research.keep(store, run, development_files(rows, development_report(rows)), IDENTITY)
    path = tmp_path / research.where(run) / research.PREDICTIONS
    path.write_bytes(lb.parquet(rows.head(3)))
    with pytest.raises(ValueError, match="don't match the manifest"):
        research.read(store, run)


def test_a_hockey_run_reproduces_its_report(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    models = [("B2", "none"), ("B3", "none"), ("B3+arena", "none")]
    rows = predictions(models=models).filter(pl.col("season").is_in(SEASONS))
    rows = rows.with_columns(experiment=pl.lit("hockey"))
    fits: dict[str, dict[int, Any]] = {"hockey": {}, "B3+arena": {}}
    report = b3_report.hockey(
        rows, {"hockey": coverage()["E1"]}, fits, flags(rows), SEASONS, "backtest-hockey-x", NOW
    )
    assert "paired_against_B3" in report["models"]["B3+arena"]
    bodies = research.files(
        rows, SEASONS, {"hockey": coverage()["E1"]}, report, {}, flags=flags(rows)
    )
    # The variant's name is what the report pairs on, so the kept fits carry it.
    bodies[research.FITS] = research._json({"b3": {"hockey": {}, "B3+arena": {}}})
    run = "backtest-hockey-x-180509"
    assert research.keep(store, run, bodies, {"mode": research.HOCKEY}) == []
    assert research.reproduce(research.read(store, run)) == []


class Tables:
    def __init__(self) -> None:
        self.frames = {
            "games": pl.DataFrame({"game_id": [1, 2]}),
            "stints": pl.DataFrame({"x": [1]}),
        }

    def read(self, table: str, seasons: Any = None) -> pl.DataFrame:
        return self.frames[table]


def test_every_read_is_recorded_by_the_sha256_of_its_rows() -> None:
    lake = research.Recorded(Tables())
    games = lake.read("games")
    lake.read("stints", seasons=[20212022, 20182019])
    assert lake.reads == {
        "games": lb.sha256(lb.parquet(games)),
        "stints:20182019,20212022": lb.sha256(lb.parquet(pl.DataFrame({"x": [1]}))),
    }


def test_compare_pairs_two_runs_game_by_game_and_names_what_they_read_differently(
    tmp_path: Path,
) -> None:
    store = lb.LocalStore(tmp_path)
    rows, other = predictions(), predictions(seed=4)
    first, second = "backtest-20261021-abc1234-180509", "backtest-20261021-abc1234-181200"
    research.keep(store, first, development_files(rows, development_report(rows)), IDENTITY)
    bodies = development_files(other, development_report(other))
    bodies[research.INPUTS] = research._json({"games": "a" * 64, "sbr_odds": "c" * 64})
    research.keep(store, second, bodies, IDENTITY)
    compared = research.compare(
        research.read(store, first), research.read(store, second), "B3", "B3", "E1"
    )
    # The same games of the tested seasons, the training fold left out.
    assert compared["games"] == {"base": 2 * GAMES, "other": 2 * GAMES, "paired": 2 * GAMES}
    keys = ["season", "game_id", "game_date"]
    a = rows.filter(pl.col("model") == "B3", pl.col("season").is_in(SEASONS))
    b = other.filter(pl.col("model") == "B3", pl.col("season").is_in(SEASONS))
    direct = (
        a.select(*keys, base="log_loss")
        .join(b.select(*keys, other="log_loss"), on=keys)
        .with_columns(difference=pl.col("other") - pl.col("base"))
    )
    assert compared["difference"] == reports._estimates(direct, "difference")
    assert compared["inputs_differ"] == ["sbr_odds"]
    with pytest.raises(ValueError, match="has no E1 B9 none"):
        research.compare(
            research.read(store, first), research.read(store, second), "B3", "B9", "E1"
        )
