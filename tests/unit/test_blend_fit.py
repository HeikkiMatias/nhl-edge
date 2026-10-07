import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import numpy as np
import polars as pl
import pytest
from blend_fixtures import rows

from nhl_edge.backtest import blend as blend_backtest
from nhl_edge.backtest.walk_forward import B1_METHOD
from nhl_edge.game import b2, b3, uncertainty
from nhl_edge.live import blend_fit as bf
from nhl_edge.market import recalibration

SEASONS = [20182019, 20192020, 20202021, 20212022, 20222023]
ROWS = rows(SEASONS, games=300)
LIVE_START = datetime(2026, 9, 29, 23, tzinfo=UTC)
NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
B1 = recalibration.Recalibration(-0.03, 1.1, 9_000, datetime(2023, 4, 30, 10, tzinfo=UTC))


def fitted() -> tuple[bf.Fold, dict[str, Any]]:
    fold = bf.fit(ROWS, SEASONS, LIVE_START, bf.LIVE_SEASON)
    first = ROWS.filter(pl.col("season") == 20222023)["prediction_utc"].min()
    assert isinstance(first, datetime)
    reproduced = bf.describe(bf.fit(ROWS, SEASONS[:4], first, 20222023))
    return fold, bf.artifact("blend-live-20261007-abc1234", fold, B1, LIVE_START, NOW, reproduced)


def test_the_artifact_reads_back_as_the_fit_it_records() -> None:
    fold, record = fitted()
    # Through the file exactly as nhl live blend-fit writes it, keys sorted.
    live = bf.load(json.loads(json.dumps(record, indent=2, sort_keys=True)))
    for name, entry in record["fits"].items():
        assert live.blends[name].named() == entry["weights"]
        assert live.blends[name].weights == fold.blends[name].weights
    assert live.version == "blend-live-20261007-abc1234"
    assert live.fold_start == LIVE_START
    assert live.train_cutoff < LIVE_START
    assert live.b1 == B1
    assert live.scale.means == fold.scale.means and live.scale.u_sd == fold.scale.u_sd
    sample = ROWS.head(50)
    u = live.scale.score(sample).to_numpy()
    for name, source in blend_backtest.MODELS.items():
        p_model = None if source is None else sample[f"p_{source.lower()}"].to_numpy()
        args = (sample["p_mkt"].to_numpy(), p_model, None if source is None else u)
        np.testing.assert_allclose(
            live.blends[name].predict(*args), fold.blends[name].predict(*args), rtol=1e-12
        )
    assert record["training"]["games"] == ROWS.height
    assert record["training"]["per_season"] == {str(s): 300 for s in SEASONS}
    assert record["experiment"] == "E1" and record["season"] == 20262027


def test_a_fit_reading_past_the_fold_start_is_not_recorded() -> None:
    fold, _ = fitted()
    with pytest.raises(ValueError, match="after"):
        bf.artifact("blend-live-x", fold, B1, fold.scale.train_cutoff, NOW, {})


def test_the_reproduction_must_match_the_recorded_fold() -> None:
    _, record = fitted()
    recorded = record["reproduction"]["fits"]
    assert bf.differences(recorded, recorded) == []
    nudged = json.loads(json.dumps(recorded))
    nudged["BLEND"]["weights"]["b_x"] += 1e-6
    nudged["BLEND_MARKET"]["games"] += 1
    assert bf.differences(nudged, recorded) == [
        "BLEND weights b_x: "
        f"{nudged['BLEND']['weights']['b_x']} against {recorded['BLEND']['weights']['b_x']}",
        f"BLEND_MARKET games: {recorded['BLEND_MARKET']['games'] + 1} against "
        f"{recorded['BLEND_MARKET']['games']}",
    ]


def test_the_recorded_2022_23_fold_is_read_from_the_one_run() -> None:
    recorded = bf.recorded()
    assert set(recorded) == set(blend_backtest.MODELS)
    assert recorded["BLEND"]["games"] == 4533
    assert recorded["BLEND"]["weights"]["b_m"] == pytest.approx(0.670, abs=1e-3)


def test_season_predictions_are_never_scored(monkeypatch: pytest.MonkeyPatch) -> None:
    season, start = 20222023, datetime(2022, 10, 7, 17, tzinfo=UTC)
    priced = pl.DataFrame(
        {
            "season": [season, season, 20212022],
            "game_id": [2022020001, 2022020002, 2021020001],
            "game_date": [date(2022, 10, 7), date(2022, 10, 8), date(2021, 10, 12)],
            "prediction_utc": [
                datetime(2022, 10, 7, 18, tzinfo=UTC),
                datetime(2022, 10, 8, 23, tzinfo=UTC),
                datetime(2021, 10, 12, 23, tzinfo=UTC),
            ],
            "home_win": [1, 0, 1],
            "p_home": [0.55, 0.48, 0.6],
        },
        schema_overrides={"season": pl.Int32, "home_win": pl.Int8},
    )
    calls: dict[str, Any] = {}

    def model(name: str) -> Any:
        def predictions(tables: Any, moments: pl.DataFrame, s: int, cut: datetime, *_: Any) -> Any:
            calls[name] = (moments, s, cut)
            frame = moments.select(
                "game_id",
                p_home=pl.lit(0.5),
                train_cutoff=pl.lit(datetime(2022, 5, 2, 10, tzinfo=UTC)),
            )
            return frame, None

        return predictions

    def parts(tables: Any, moments: pl.DataFrame) -> pl.DataFrame:
        calls["parts"] = moments
        return moments.select("game_id", *[pl.lit(0.1).alias(p) for p in uncertainty.PARTS])

    monkeypatch.setattr(b2, "predictions", model("B2"))
    monkeypatch.setattr(b3, "predictions", model("B3"))
    monkeypatch.setattr(uncertainty, "parts", parts)
    predicted, doubts = bf.season_predictions(None, None, None, priced, season, start)  # type: ignore[arg-type]
    # The season's priced games at puck drop, with fits cut off at the fold start.
    for name in ("B2", "B3"):
        moments, s, cut = calls[name]
        assert moments["game_id"].to_list() == [2022020001, 2022020002]
        assert (s, cut) == (season, start)
    assert calls["parts"].equals(calls["B3"][0])
    assert predicted.columns == list(bf.PREDICTION_COLUMNS)
    assert "log_loss" not in predicted.columns
    assert sorted(set(predicted["model"].to_list())) == ["B0", "B2", "B3"]
    assert set(predicted.filter(pl.col("model") == "B0")["method"]) == {B1_METHOD.value}
    assert set(doubts["season"]) == {season}


class FakeLake:
    """A lake whose tables are empty, but for the SBR seasons the fit asks for."""

    def read(self, table: str, seasons: Any = None) -> pl.DataFrame:
        if table == "sbr_odds":
            return pl.DataFrame({"season": SEASONS}, schema={"season": pl.Int32})
        return pl.DataFrame({"season": []}, schema={"season": pl.Int32})


def test_the_dry_run_writes_only_its_fit(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """nhl live blend-fit without --r2: one file under --out, nothing under reports/backtest/, no
    claim. The rows and models are faked; the fit, the check and the artifact are the real ones."""
    from typer.testing import CliRunner

    from nhl_edge import cli
    from nhl_edge.ingest import games as games_module
    from nhl_edge.lake import tables

    firsts = {s: ROWS.filter(pl.col("season") == s)["prediction_utc"].min() for s in SEASONS}
    reference = bf.describe(bf.fit(ROWS, SEASONS[:4], firsts[20222023], 20222023))  # type: ignore[arg-type]
    monkeypatch.chdir(tmp_path)
    backtest = tmp_path / "reports" / "backtest"
    backtest.mkdir(parents=True)
    (backtest / "runs.csv").write_text("version,seasons\n")
    monkeypatch.setattr(tables, "Lake", FakeLake)
    monkeypatch.setattr(games_module, "EXPECTED_GAMES", dict.fromkeys(SEASONS, 0))
    spare = SimpleNamespace(lineups=None, lineup_replacements=None)
    monkeypatch.setattr(cli, "_b3_tables", lambda lake, t: spare)
    monkeypatch.setattr(b2, "input_problems", lambda *a: [])
    monkeypatch.setattr(b3, "input_problems", lambda *a: [])
    monkeypatch.setattr(uncertainty, "Tables", lambda *a: None)
    monkeypatch.setattr(bf, "market", lambda odds, games: ROWS.select("season", "game_id"))
    monkeypatch.setattr(
        bf, "fold_start", lambda odds, games, season: firsts.get(season, LIVE_START)
    )
    empty = pl.DataFrame({"model": []}, schema={"model": pl.String})
    monkeypatch.setattr(bf, "season_predictions", lambda *a: (empty, empty))
    monkeypatch.setattr(blend_backtest, "rows", lambda *a: ROWS)
    monkeypatch.setattr(bf, "recorded", lambda: reference)
    monkeypatch.setattr(bf, "recorded_coverage", lambda: {"b3_scored": 300})
    monkeypatch.setattr(bf, "digest", lambda: "0" * 64)
    monkeypatch.setattr(bf, "b1_fit", lambda priced, start: B1)
    result = CliRunner().invoke(cli.app, ["live", "blend-fit", "--out", "dry"])
    assert result.exit_code == 0, result.output
    written = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    assert [str(p) for p in written if "dry" in str(p)] == [
        f"dry/{p.name}" for p in (tmp_path / "dry").iterdir()
    ]
    assert sorted(str(p) for p in written if "dry" not in str(p)) == ["reports/backtest/runs.csv"]
    assert (backtest / "runs.csv").read_text() == "version,seasons\n"
    record = json.loads(next((tmp_path / "dry").iterdir()).read_text())
    assert record["training"]["games"] == ROWS.height
    assert "log_loss" not in json.dumps(record)


def test_rows_short_of_the_priced_games_or_the_recorded_run_are_refused() -> None:
    priced = ROWS.select("season", "game_id")
    coverage = {"b3_scored": 300}
    assert bf.short(ROWS, priced, coverage) == []
    dropped = ROWS.filter(
        pl.col("game_id") != ROWS.filter(pl.col("season") == 20222023)["game_id"][0]
    )
    assert bf.short(dropped, priced, coverage) == [
        "20222023: 299 rows of 300 priced games",
        "20222023: 299 rows, its one run scored 300",
    ]
    assert bf.short(ROWS, priced, {"b3_scored": 342}) == [
        "20222023: 300 rows, its one run scored 342"
    ]


def test_the_recorded_run_covered_342_games_of_2022_23() -> None:
    assert bf.recorded_coverage()["b3_scored"] == 342
    assert bf.committed()
