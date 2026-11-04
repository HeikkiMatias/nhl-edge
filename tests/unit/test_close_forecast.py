"""The close forecaster (#369, ADR 0054): the pinned weights, the week and its fold start, the
week's size, B3's parts from a morning bundle, the day's forecasts, the record and its replay."""

import json
import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import forecast_fixtures as fx
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
    for spec in cf.VERSIONS:
        altered = tmp_path / spec.weights.name
        record = json.loads(spec.weights.read_text())
        record["weights"]["a"] += 0.01
        altered.write_text(json.dumps(record))
        with pytest.raises(ValueError, match="not the weights"):
            cf.weights(replace(spec, weights=altered))
    # By default, version 1's.
    monkeypatch.setattr(cf, "V1", replace(cf.V1, weights=tmp_path / cf.WEIGHTS_2.name))
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


# Version 2 (ADR 0056)


def fit2(lam: float = 0.5, lam_d: float = 2.0, week: date = date(2026, 11, 2)) -> cf.Fit:
    rows = pl.DataFrame(schema=cf.ROW_SCHEMA_2)
    return cf.Fit(week, cf.fold_start(week), lam, 0.01, rows, lam_d, cf.VERSION_2)


def test_version_2s_weights_are_pinned_with_densitys_beside_the_split() -> None:
    w = cf.weights(cf.V2)
    assert w.version == cf.WEIGHTS_2.stem and w.version.startswith("close-forecast-2-")
    assert w.train_cutoff == datetime(2018, 4, 9, 10, tzinfo=UTC)
    assert list(w.parts) == list(b3.SPLIT) and list(w.density) == list(fx.DENSITY)
    # Version 1's weights carry no density, and its record is its own.
    assert cf.weights().density == {} and cf.weights().version != w.version
    assert [s.version for s in cf.VERSIONS] == [cf.VERSION, cf.VERSION_2]
    assert cf.registered(cf.REGISTERED_ON_2 - timedelta(days=1)) == (cf.V1,)
    assert cf.registered(cf.REGISTERED_ON_2) == (cf.V1, cf.V2)


def test_the_two_sizes_are_the_prior_without_rows_and_the_data_with_many() -> None:
    sizes, variance = cf.fit_sizes(np.empty((0, 2)), np.array([]))
    assert list(sizes) == [cf.PRIOR_MEAN, cf.PRIOR_MEAN] and variance is None
    rng = np.random.default_rng(3)
    x = rng.normal(0, 0.05, (3000, 2))
    y = x @ np.array([0.4, 1.6]) + rng.normal(0, 0.01, 3000)
    sizes, variance = cf.fit_sizes(x, y)
    assert sizes == pytest.approx([0.4, 1.6], abs=0.03)
    assert variance == pytest.approx(0.01**2, rel=0.1)
    # With one column it is version 1's lam.
    one, _ = cf.fit_sizes(x[:, :1], y)
    assert one[0] == pytest.approx(cf.fit_lam(x[:, 0], y)[0])
    few, _ = cf.fit_sizes(x[:4], rng.normal(0, 0.1, 4))
    assert np.all(np.abs(few - cf.PRIOR_MEAN) < 1.0)


def test_density_is_counted_from_the_seasons_games_known_at_the_morning_cutoff() -> None:
    counted = cf.density_inputs(fx.slate_bundle(), fx.played(fx.EARLIER))
    rows = {r["game_id"]: r for r in counted.iter_rows(named=True)}
    bos, tor, mtl = rows[2026020100], rows[2026020101], rows[2026020102]
    # BOS played at PHI last night, NYR at home two nights ago.
    assert (bos["dense3_diff"], bos["dense6_diff"], bos["mismatch"]) == (0.0, 0.0, -1.0)
    assert (bos["home_return"], bos["away_trip"]) == (1.0, 0.0)
    # TOR comes home from two road games; OTT hasn't played.
    assert (tor["dense3_diff"], tor["dense6_diff"], tor["home_return"]) == (1.0, 2.0, 2.0)
    assert tor["mismatch"] == 0.0
    assert all(mtl[c] == 0.0 for c in fx.DENSITY)
    assert sorted(rows) == sorted(fx.morning()["game_id"].to_list())


def test_version_2_moves_the_morning_price_by_both_sizes() -> None:
    w = cf.weights(cf.V2)
    given = fx.with_density(found(), {"mismatch": [1.0, 0.0, -1.0], "away_trip": [2.0, 0.0, 0.0]})
    rows = cf.forecast(morning(), given, w, fit2(0.5, 2.0), PUBLISHED, "v", cf.V2)
    assert rows["status"].to_list() == [cf.PREDICTED] * 3
    first = rows.row(0, named=True)
    o = math.log(0.55 / 0.45)
    hist = w.a + w.b_o * o + 0.2 * w.parts["five_diff"] + 0.05 * w.parts["special_diff"]
    hist += 0.1 * w.parts["goalie_diff"]
    dens = w.density["mismatch"] + 2.0 * w.density["away_trip"]
    assert first["y_hat_hist"] == pytest.approx(hist)
    assert first["y_hat_density"] == pytest.approx(dens)
    assert first["y_hat"] == pytest.approx(0.5 * hist + 2.0 * dens)
    assert first["p_close_hat"] == pytest.approx(1 / (1 + math.exp(-(o + 0.5 * hist + 2 * dens))))
    assert (first["lam"], first["lam_d"]) == (0.5, 2.0)
    assert first["forecast_version"] == cf.VERSION_2 and first["weights_version"] == w.version
    assert first["mismatch"] == 1.0 and first["away_trip"] == 2.0
    # Version 1's rows leave version 2's columns null.
    plain = cf.forecast(morning(), given, cf.weights(), fit(), PUBLISHED, "v")
    assert plain["y_hat_density"].null_count() == 3 and plain["lam_d"].null_count() == 3
    assert plain["mismatch"].null_count() == 3
    CloseForecasts.validate(pl.concat([rows, plain]))


def test_version_2_without_density_has_no_forecast_and_needs_the_columns() -> None:
    w = cf.weights(cf.V2)
    gap = pl.when(pl.col("game_id") == 2026020101).then(None).otherwise(pl.col("mismatch"))
    given = fx.with_density(found()).with_columns(mismatch=gap)
    rows = cf.forecast(morning(), given, w, fit2(), PUBLISHED, "v", cf.V2)
    assert rows["status"].to_list() == [cf.PREDICTED, cf.NO_DENSITY, cf.PREDICTED]
    with pytest.raises(ValueError, match="needs"):
        cf.forecast(morning(), fx.found(), w, fit2(), PUBLISHED, "v", cf.V2)


def test_each_version_is_written_once_under_its_own_prefix(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    one = cf.forecast(morning(), found(), cf.weights(), fit(), PUBLISHED, "v")
    two = cf.forecast(
        morning(), fx.with_density(found()), cf.weights(cf.V2), fit2(), PUBLISHED, "v", cf.V2
    )
    assert cf.write_once(store, DAY, one) == "forecast/live/2026-11-04.parquet"
    assert cf.write_once(store, DAY, two, cf.V2) == "forecast/live-2/2026-11-04.parquet"
    with pytest.raises(ValueError, match="can't be written"):
        cf.write_once(store, date(2026, 11, 5), two)
    cf.keep_week(store, fit(), "w")
    cf.keep_week(store, fit2(), "w2")
    assert cf.stored_days(store) == [DAY] and cf.stored_days(store, cf.V2) == [DAY]
    synced = cf.sync(store)
    assert synced.height == 6
    assert sorted(set(synced["forecast_version"].to_list())) == [cf.VERSION, cf.VERSION_2]
    # Each version's week is its own, read back with its sizes.
    kept = cf.kept_week(store, date(2026, 11, 2), cf.V2)
    assert kept is not None and (kept.lam, kept.lam_d, kept.version) == (0.5, 2.0, cf.VERSION_2)
    record = json.loads((tmp_path / cf.week_keys(date(2026, 11, 2), cf.V2)[0]).read_text())
    assert record["forecast_version"] == cf.VERSION_2 and record["lam_d"] == 2.0
    first = cf.kept_week(store, date(2026, 11, 2))
    assert first is not None and first.lam_d is None and first.version == cf.VERSION


def test_a_day_stored_before_version_2_still_reads_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    store.put(lm.key(DAY), lb.parquet(morning()))
    monkeypatch.setattr(lb, "read", lambda *_: None)
    monkeypatch.setattr(cf, "parts", lambda _: found())
    cf.keep_week(store, fit(cf.PRIOR_MEAN), cf.weights().version)
    rows = cf.forecast(morning(), found(), cf.weights(), fit(cf.PRIOR_MEAN), PUBLISHED, "v")
    # As version 1 stored it before version 2's columns existed.
    old = rows.drop("dense3_diff", "dense6_diff", "mismatch", "home_return", "away_trip")
    old = old.drop("y_hat_density", "lam_d")
    (tmp_path / "forecast/live").mkdir(parents=True, exist_ok=True)
    (tmp_path / cf.key(DAY)).write_bytes(lb.parquet(old))
    assert cf.stored(store, DAY).equals(rows)
    assert cf.check(store, DAY) == []


def test_version_2s_day_replays_and_a_change_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    store.put(lm.key(DAY), lb.parquet(morning()))
    monkeypatch.setattr(lb, "read", lambda *_: fx.slate_bundle())
    monkeypatch.setattr(cf, "parts", lambda _: found())
    games = fx.played(fx.EARLIER)
    w = cf.weights(cf.V2)
    prior = fit2(cf.PRIOR_MEAN, cf.PRIOR_MEAN)
    cf.keep_week(store, prior, w.version)
    found_2 = cf.inputs(fx.slate_bundle(), cf.V2, lambda: games)
    rows = cf.forecast(morning(), found_2, w, prior, PUBLISHED, "v", cf.V2)
    cf.write_once(store, DAY, rows, cf.V2)
    assert cf.check(store, DAY, cf.V2, lambda: games) == []
    # Without the season's games, version 2 can't replay; version 1's day isn't there.
    assert "reads the season's games" in cf.check(store, DAY, cf.V2)[0]
    assert len(cf.check(store, DAY)) == 1
    # A game the lake later shows played before the cutoff changes density, and shows.
    more = pl.concat([games, fx.played([(2026020087, date(2026, 11, 3), "DET", "MTL")])])
    assert cf.check(store, DAY, cf.V2, lambda: more)
