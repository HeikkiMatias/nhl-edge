"""The close forecaster (#369, ADR 0054): the pinned weights, the week and its fold start, the
week's size, B3's parts from a morning bundle, the day's forecasts, the record and its replay."""

import json
import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from forecast_fixtures import DAY, PUBLISHED, found, morning

from nhl_edge.game import b3
from nhl_edge.lake.schemas import CloseForecasts
from nhl_edge.live import bundle as lb
from nhl_edge.live import close_forecast as cf
from nhl_edge.live import morning as lm


def fit(lam: float = 0.5, week: date = date(2026, 11, 2)) -> cf.Fit:
    return cf.Fit(week, cf.fold_start(week), lam, 0.01, pl.DataFrame(schema=cf.ROW_SCHEMA))


def test_the_weights_are_pinned_and_refused_when_altered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = cf.weights()
    assert w.version == cf.WEIGHTS.stem
    assert w.train_cutoff == datetime(2018, 4, 9, 10, tzinfo=UTC)
    assert list(w.parts) == list(b3.SPLIT)
    altered = tmp_path / cf.WEIGHTS.name
    record = json.loads(cf.WEIGHTS.read_text())
    record["weights"]["a"] += 0.01
    altered.write_text(json.dumps(record))
    monkeypatch.setattr(cf, "WEIGHTS", altered)
    with pytest.raises(ValueError, match="not the weights"):
        cf.weights()


def test_the_week_starts_on_monday_and_its_fold_at_07_00_et() -> None:
    assert cf.week_of(DAY) == date(2026, 11, 2)
    assert cf.week_of(date(2026, 11, 8)) == date(2026, 11, 2)
    assert cf.week_of(date(2026, 11, 9)) == date(2026, 11, 9)
    # 07:00 EST is 12:00 UTC; in daylight time, 11:00 UTC.
    assert cf.fold_start(date(2026, 11, 2)) == datetime(2026, 11, 2, 12, tzinfo=UTC)
    assert cf.fold_start(date(2026, 10, 26)) == datetime(2026, 10, 26, 11, tzinfo=UTC)


def test_the_size_is_the_prior_without_rows_and_the_data_with_many() -> None:
    assert cf.fit_lam(np.array([]), np.array([])) == (cf.PRIOR_MEAN, None)
    rng = np.random.default_rng(2)
    y_hat = rng.normal(0, 0.05, 2000)
    y = 0.4 * y_hat + rng.normal(0, 0.01, 2000)
    lam, variance = cf.fit_lam(y_hat, y)
    assert lam == pytest.approx(0.4, abs=0.02)
    assert variance == pytest.approx(0.01**2, rel=0.1)
    # With a few noisy rows, the prior holds lam near 1.
    few, _ = cf.fit_lam(y_hat[:5], rng.normal(0, 0.1, 5))
    assert abs(few - cf.PRIOR_MEAN) < abs(few - 0.0)


def test_the_parts_are_b3s_split_mixed_over_the_goalie_pairs() -> None:
    from b3_fixtures import day_bundle, league

    tables = league()
    bundle = day_bundle(tables)
    plain = b3.fit_fold(
        tables,
        20182019,
        bundle.inputs["slate"]["start_utc"].min(),  # type: ignore[arg-type]
    )
    parts = cf.parts(replace(bundle, b3=plain))
    assert parts.height == bundle.inputs["slate"].height
    _, pairs = lb.b3_scenarios(bundle)
    mixed = pairs.group_by("game_id").agg(
        d=(pl.col("delta_g_hat") * pl.col("weight")).sum() / pl.col("weight").sum()
    )
    joined = parts.join(mixed, on="game_id")
    total = joined.select(sum(pl.col(p) for p in b3.SPLIT))["literal"].to_numpy()
    # The four parts sum to B3's whole difference, mixed as B3 mixes it (ADR 0044).
    assert np.allclose(total, joined["d"].to_numpy())
    assert cf.parts(bundle).is_empty()


def test_the_forecast_moves_the_morning_price_by_the_weeks_size() -> None:
    w = cf.weights()
    rows = cf.forecast(morning(), found(), w, fit(0.5), PUBLISHED, "close-forecast-x")
    assert rows["status"].to_list() == [cf.PREDICTED] * 3
    first = rows.row(0, named=True)
    o = math.log(0.55 / 0.45)
    hist = w.a + w.b_o * o + 0.2 * w.parts["five_diff"] + 0.05 * w.parts["special_diff"]
    hist += 0.1 * w.parts["goalie_diff"]
    assert first["y_hat_hist"] == pytest.approx(hist)
    assert first["y_hat"] == pytest.approx(0.5 * hist)
    assert first["p_close_hat"] == pytest.approx(1 / (1 + math.exp(-(o + 0.5 * hist))))
    assert first["lam"] == 0.5 and first["lam_week"] == date(2026, 11, 2)
    assert first["lam_games"] == 0 and first["weights_version"] == w.version
    assert first["forecast_version"] == cf.VERSION and first["morning_version"] == lm.VERSION
    CloseForecasts.validate(rows)


def test_a_game_without_its_morning_or_parts_or_that_starts_first_has_no_forecast() -> None:
    w = cf.weights()
    day = morning(statuses=("no Pinnacle morning price", "predicted", "predicted"))
    day = day.with_columns(
        start_utc=pl.when(pl.col("game_id") == 2026020102)
        .then(pl.lit(PUBLISHED))
        .otherwise(pl.col("start_utc"))
    )
    rows = cf.forecast(day, found([2026020100, 2026020102]), w, fit(), PUBLISHED, "v")
    assert rows["status"].to_list() == [cf.NO_MORNING, cf.NO_PARTS, cf.STARTS_FIRST]
    assert rows["p_close_hat"].null_count() == 3
    assert rows["p_morning"].to_list()[0] is None


def test_the_day_is_written_once_and_rebuilt(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    rows = cf.forecast(morning(), found(), cf.weights(), fit(), PUBLISHED, "v")
    assert cf.write_once(store, DAY, rows) == "forecast/live/2026-11-04.parquet"
    # The same bytes count as written; other bytes are refused.
    cf.write_once(store, DAY, rows)
    later = cf.forecast(morning(), found(), cf.weights(), fit(), PUBLISHED + timedelta(1), "v")
    with pytest.raises(lb.Conflict):
        cf.write_once(store, DAY, later)
    cf.keep_week(store, fit(), "w")
    assert cf.stored_days(store) == [DAY]
    assert cf.sync(store).equals(rows)


def test_the_weeks_size_is_fitted_once_and_read_back(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    week = date(2026, 11, 2)
    calls: list[str] = []

    def source(name: str) -> pl.DataFrame:
        calls.append(name)
        return pl.DataFrame()

    kept = cf.Fit(week, cf.fold_start(week), 0.7, 0.002, pl.DataFrame(schema=cf.ROW_SCHEMA))
    cf.keep_week(store, kept, "w")
    again = cf.week_fit(
        store, week, lambda: source("f"), lambda: source("m"), lambda: source("o"), "w"
    )
    assert calls == [] and again.lam == 0.7 and again.rows.height == 0
    record = json.loads((tmp_path / cf.week_keys(week)[0]).read_text())
    assert record["forecast_version"] == cf.VERSION and record["games"] == 0
    assert cf.kept_week(store, date(2026, 11, 9)) is None


def test_a_stored_day_replays_and_a_change_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    store.put(lm.key(DAY), lb.parquet(morning()))
    monkeypatch.setattr(lb, "read", lambda *_: None)
    monkeypatch.setattr(cf, "parts", lambda _: found())
    # A week without earlier rows keeps the prior's size.
    cf.keep_week(store, fit(cf.PRIOR_MEAN), cf.weights().version)
    rows = cf.forecast(morning(), found(), cf.weights(), fit(cf.PRIOR_MEAN), PUBLISHED, "v")
    cf.write_once(store, DAY, rows)
    assert cf.check(store, DAY) == []
    moved = rows.with_columns(p_close_hat=pl.col("p_close_hat") * 0.99)
    (tmp_path / cf.key(DAY)).write_bytes(lb.parquet(moved))
    assert cf.check(store, DAY) == [
        "2026-11-04: p_close_hat differs for " + str(sorted(rows["game_id"]))
    ]
    # A day with no stored forecast, or without its week's fit, can't be replayed.
    assert len(cf.check(store, date(2026, 11, 10))) == 1
    (tmp_path / cf.week_keys(date(2026, 11, 2))[0]).unlink()
    assert "fit is not kept" in cf.check(store, DAY)[0]


def test_a_week_without_earlier_forecasts_reads_no_mornings_or_odds(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)

    def refused() -> pl.DataFrame:
        raise AssertionError("read without an earlier forecast")

    first = cf.week_fit(store, date(2026, 11, 2), cf.empty, refused, refused, "w")
    assert first.lam == cf.PRIOR_MEAN and first.rows.height == 0
    assert cf.kept_week(store, date(2026, 11, 2)) is not None
