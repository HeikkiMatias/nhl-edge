import numpy as np
import polars as pl
import pytest
from blend_fixtures import WEIGHTS, rows, starts

from nhl_edge.backtest import blend as blend_backtest
from nhl_edge.market import blend
from nhl_edge.market.blend import Kind

SEASONS = [20182019, 20192020, 20202021, 20212022]


def test_the_fit_recovers_known_weights() -> None:
    frame = rows(SEASONS, games=6000)
    from nhl_edge.game import uncertainty

    scale = uncertainty.fit_scale(
        frame.select(*uncertainty.PARTS, observed_utc="parts_utc", train_cutoff="parts_cutoff")
    )
    u = scale.score(frame).to_numpy()
    fitted = blend.fit(
        Kind.MODEL,
        frame["home_win"].to_numpy(),
        frame["p_mkt"].to_numpy(),
        frame["result_utc"].max(),  # type: ignore[arg-type]
        frame["p_b3"].to_numpy(),
        u,
    )
    named = fitted.named()
    for term, value in WEIGHTS.items():
        assert named[term] == pytest.approx(
            value, abs=4 * fitted.standard_errors[list(named).index(term)]
        )
    assert fitted.games == frame.height


def test_the_market_control_has_two_weights_and_reads_no_model() -> None:
    frame = rows([20182019], games=2000)
    fitted = blend.fit(
        Kind.MARKET,
        frame["home_win"].to_numpy(),
        frame["p_mkt"].to_numpy(),
        frame["result_utc"].max(),  # type: ignore[arg-type]
    )
    assert set(fitted.named()) == {"a", "b_m"}
    p = fitted.predict(frame["p_mkt"].to_numpy())
    assert p.shape == (frame.height,) and np.all((p > 0) & (p < 1))


def test_the_fit_refuses_what_it_cannot_fit() -> None:
    frame = rows([20182019], games=50)
    cutoff = frame["result_utc"].max()
    with pytest.raises(ValueError, match="both home wins and home losses"):
        blend.fit(Kind.MARKET, np.ones(50), frame["p_mkt"].to_numpy(), cutoff)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="needs p_model and u"):
        blend.fit(Kind.MODEL, frame["home_win"].to_numpy(), frame["p_mkt"].to_numpy(), cutoff)  # type: ignore[arg-type]


def test_each_tested_season_gets_three_blends_on_the_same_games() -> None:
    every = rows(SEASONS, experiments=("E1", "E2"))
    predictions, fits, scales, coverage = blend_backtest.run(
        every, starts(every), [20182019, 20212022]
    )
    # 2018-19 has no earlier fold, so no blend.
    assert set(predictions["season"].unique()) == {20212022}
    assert set(predictions["model"].unique()) == set(blend_backtest.MODELS)
    for experiment in ("E1", "E2"):
        counts = coverage[experiment][20212022]
        assert counts == {"blend_scored": 600, "blend_trained_on": 1800}
        assert set(fits[experiment][20212022]) == set(blend_backtest.MODELS)
        assert scales[experiment][20212022].games == 1800
        by_model = (
            predictions.filter(experiment=experiment)
            .group_by("model")
            .agg(pl.col("game_id").sort())
        )
        ids = by_model["game_id"].to_list()
        assert all(i == ids[0] for i in ids)
    assert predictions["log_loss"].is_not_null().all()


def test_a_term_that_never_varies_keeps_a_weight_of_zero() -> None:
    frame = rows([20182019], games=2000)
    fitted = blend.fit(
        Kind.MODEL,
        frame["home_win"].to_numpy(),
        frame["p_mkt"].to_numpy(),
        frame["result_utc"].max(),  # type: ignore[arg-type]
        frame["p_b3"].to_numpy(),
        np.zeros(frame.height),
    )
    assert fitted.named()["b_u"] == 0.0
    assert np.isnan(fitted.standard_errors[3])
    assert fitted.named()["b_x"] != 0.0


def test_a_term_the_market_already_spans_keeps_a_weight_of_zero() -> None:
    frame = rows([20182019], games=2000)
    fitted = blend.fit(
        Kind.MODEL,
        frame["home_win"].to_numpy(),
        frame["p_mkt"].to_numpy(),
        frame["result_utc"].max(),  # type: ignore[arg-type]
        frame["p_mkt"].to_numpy(),  # a model that is the market itself
        np.ones(frame.height),
    )
    named = fitted.named()
    assert named["b_x"] == 0.0 and named["b_u"] == 0.0
    assert named["b_m"] != 0.0
