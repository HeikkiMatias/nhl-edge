"""scripts/b3_stationarity.py (#378 step 1): the unpenalized fit, Cochran's Q, the weekly block
bootstrap and the step-2 decision, on made-up games."""

import numpy as np
import pytest
from b3_stationarity_fixtures import games, load

st = load()
STABLE = {season: (0.45, 0.05) for season in st.SEASONS}


def test_the_fit_recovers_the_coefficient_and_the_intercept() -> None:
    rows = games({20132014: (0.5, 0.1)}, per_season=20000)
    found = st.fit(rows, 20132014)
    assert found.games == 20000
    assert found.coefficient == pytest.approx(0.5, abs=0.06)
    assert found.intercept == pytest.approx(0.1, abs=0.04)


def test_cochran_q_by_hand() -> None:
    found = st.cochran_q([1.0, 2.0, 3.0], [1.0, 1.0, 4.0])
    mean = (1 + 2 + 3 / 4) / 2.25
    q = (1 - mean) ** 2 + (2 - mean) ** 2 + (3 - mean) ** 2 / 4
    assert found["mean"] == pytest.approx(mean) and found["q"] == pytest.approx(q)
    assert found["df"] == 2 and 0 < found["p_value"] < 1
    assert st.cochran_q([1.0, 1.0], [1.0, 2.0])["p_value"] == pytest.approx(1.0)


def test_the_bootstrap_resamples_weeks_and_refits() -> None:
    rows = games({20132014: (0.5, 0.1)})
    draws = st.bootstrap(rows, 20132014, draws=30, seed=1)
    assert draws.shape == (30, 2) and np.all(np.isfinite(draws))
    assert np.std(draws[:, 0]) > 0
    assert np.array_equal(draws, st.bootstrap(rows, 20132014, draws=30, seed=1))


def test_a_stable_mapping_stops_step_2_and_a_drifting_one_goes_ahead() -> None:
    stable = st.run(games(STABLE), draws=200)
    assert set(stable["per_season"]) == {str(s) for s in st.SEASONS}
    assert not stable["step_2"]
    drifting = {s: (0.2 + 0.25 * i, 0.05) for i, s in enumerate(st.SEASONS)}
    moved = st.run(games(drifting), draws=200)
    assert moved["step_2"] and moved["holm"]["coefficient"]["passes"]
    assert "Step 2 goes ahead" in st.markdown(moved, "b3-stationarity-20261104-abc1234")
    assert "Step 2 stops" in st.markdown(stable, "b3-stationarity-20261104-abc1234")
