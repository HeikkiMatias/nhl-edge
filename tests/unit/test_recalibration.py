from datetime import UTC, datetime

import market_history
import numpy as np
import pytest

from nhl_edge.backtest.walk_forward import run
from nhl_edge.market import recalibration

CUTOFF = datetime(2021, 5, 20, 10, tzinfo=UTC)


def draws(intercept: float, slope: float, games: int = 40_000) -> tuple[np.ndarray, np.ndarray]:
    """Market probabilities and results drawn from sigmoid(intercept + slope * logit(p))."""
    rng = np.random.default_rng(7)
    p = rng.uniform(0.25, 0.75, games)
    truth = recalibration.sigmoid(intercept + slope * recalibration.logit(p))
    return p, (rng.random(games) < truth).astype(np.int8)


def test_the_fit_recovers_a_known_recalibration() -> None:
    fit = recalibration.fit(*draws(0.1, 1.3), CUTOFF)
    assert fit.intercept == pytest.approx(0.1, abs=0.03)
    assert fit.slope == pytest.approx(1.3, abs=0.06)
    assert (fit.games, fit.train_cutoff) == (40_000, CUTOFF)


def test_a_calibrated_market_is_left_nearly_unchanged() -> None:
    fit = recalibration.fit(*draws(0.0, 1.0), CUTOFF)
    assert fit.intercept == pytest.approx(0.0, abs=0.03)
    assert fit.slope == pytest.approx(1.0, abs=0.05)


def test_the_identity_returns_the_market() -> None:
    identity = recalibration.Recalibration(intercept=0.0, slope=1.0, games=0, train_cutoff=CUTOFF)
    p = np.array([0.2, 0.5, 0.8])
    assert identity.predict(p) == pytest.approx(p)
    # A slope above 1 moves favourites further from even; the intercept moves every game.
    sharper = recalibration.Recalibration(intercept=0.0, slope=1.5, games=0, train_cutoff=CUTOFF)
    assert sharper.predict(p)[2] > 0.8
    assert sharper.predict(p)[0] < 0.2
    home = recalibration.Recalibration(intercept=0.2, slope=1.0, games=0, train_cutoff=CUTOFF)
    assert (home.predict(p) > p).all()


def test_the_fit_needs_both_results_and_one_per_game() -> None:
    with pytest.raises(ValueError, match="both"):
        recalibration.fit([0.4, 0.6], [1, 1], CUTOFF)
    with pytest.raises(ValueError, match="one value per game"):
        recalibration.fit([0.4, 0.6], [1], CUTOFF)


def test_the_backtest_fits_b1_per_season_on_the_seasons_before() -> None:
    odds, games = market_history.seasons([20172018, 20182019, 20192020, 20212022], games=200)
    predictions, coverage, fits = run(odds, games, [20182019, 20212022])
    for experiment in ("E1", "E2"):
        early, late = fits[experiment][20182019], fits[experiment][20212022]
        # 2018-19 is fitted on 2017-18; 2021-22 also on 2018-19 and 2019-20.
        assert (early.games, late.games) == (200, 600)
        assert early.train_cutoff == games.filter(season=20172018)["observed_utc"].max()
        assert late.train_cutoff == games.filter(season=20192020)["observed_utc"].max()
        assert coverage[experiment][20212022]["b1_trained_on"] == 600
    b1 = predictions.filter(model="B1")
    assert b1["train_cutoff"].null_count() == 0
    assert predictions.filter(model="B0")["train_cutoff"].null_count() == len(
        predictions.filter(model="B0")
    )
