"""scripts/totals_history.py (#359): the implied mean, the totals quotes, B3's projected total over
the goalie scenarios, the over at the closing line, the two measures and the confirm guard, on
SYNTHETIC data."""

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest
from scipy.stats import poisson
from totals_history_fixtures import (
    OPENED,
    PREDICTION,
    game_quotes,
    gammas,
    load,
    moments,
    pool,
    sbr,
    scored_frame,
    split,
)

from nhl_edge.game import b3
from nhl_edge.market.devig import fair_probabilities

th = load()


def test_the_implied_mean_inverts_a_poisson_total_on_half_and_whole_lines() -> None:
    half = poisson.sf(5, 5.6)
    whole = poisson.sf(5, 5.2) / (1 - poisson.pmf(5, 5.2))
    found = th.implied_mean(np.array([5.5, 5.0]), np.array([half, whole]))
    assert found == pytest.approx([5.6, 5.2], abs=1e-9)


def test_a_higher_over_price_means_a_lower_total() -> None:
    low, high = th.implied_mean(np.array([5.5, 5.5]), np.array([0.45, 0.55]))
    assert low < high


def test_quotes_are_devigged_and_odd_lines_dropped() -> None:
    rows = [
        *game_quotes(1),
        # A quarter line, unequal sides' lines, and a line out of range are dropped.
        *game_quotes(2, open_line=5.25),
        (3, "open", "over", 5.5, 1.9, OPENED),
        (3, "open", "under", 6.0, 1.9, OPENED),
        *game_quotes(4, close_line=-110.0),
    ]
    found = th.quotes(sbr(rows))
    kept = set(zip(found["game_id"].to_list(), found["quote"].to_list(), strict=True))
    assert kept == {(1, "open"), (1, "close"), (2, "close"), (4, "open")}
    opener = found.filter(pl.col("game_id") == 1, pl.col("quote") == "open")
    expected = fair_probabilities([1.95, 1.87])[0]
    assert opener["p_over"].item() == pytest.approx(expected)
    assert opener["mu"].item() == pytest.approx(th.implied_mean(np.array([5.5]), [expected])[0])


def test_the_market_has_the_move_and_the_close_in_logits() -> None:
    found = th.market(sbr(game_quotes(1)), moments([1]))
    row = found.row(0, named=True)
    assert row["y"] == pytest.approx(row["mu_c"] - row["mu_o"])
    assert row["q_c"] == pytest.approx(np.log(row["p_c"] / (1 - row["p_c"])))
    # The over got longer from the opener to the close: the market moved the total down.
    assert row["y"] < 0


def test_a_game_without_both_quotes_is_not_scored() -> None:
    rows = [r for r in game_quotes(1) if r[1] == "open"] + game_quotes(2)
    found = th.market(sbr(rows), moments([1, 2]))
    assert found["game_id"].to_list() == [2]


def test_pairs_give_b3s_delta_and_the_parts_sum_to_the_total() -> None:
    usable, candidates, table = split(), pool(), gammas()
    found = th.pairs(usable, candidates, table)
    reference = b3.scenarios(usable, candidates, table)
    # Each pair's goals give B3's own Δĝ and weight, pair by pair.
    assert found["weight"].sort().to_list() == pytest.approx(reference["weight"].sort().to_list())
    deltas = (found["home_goals"] - found["away_goals"]).sort().to_list()
    assert deltas == pytest.approx(reference["delta_g_hat"].sort().to_list())
    total = th.projection(usable, candidates, table, moments([1]))
    row = total.row(0, named=True)
    parts = sum(row[c] for c in th.PARTS)
    assert row["total"] == pytest.approx(parts)


def test_the_projection_averages_the_goalie_scenarios_by_their_weights() -> None:
    row = th.projection(split(), pool(), gammas(), moments([1])).row(0, named=True)
    home = (2.0 + 0.6) * 1.1 * 0.95
    away = (1.8 + 0.5) * 0.9 * (0.8 * 0.9 + 0.2 * 1.2)
    assert row["home_goals"] == pytest.approx(home)
    assert row["away_goals"] == pytest.approx(away)
    assert row["total"] == pytest.approx(home + away)


def test_a_team_without_candidates_faces_an_average_goalie() -> None:
    lone = pool([(1, "BOS", 30, 1.0)])
    row = th.projection(split(), lone, gammas(), moments([1])).row(0, named=True)
    assert row["home_goals"] == pytest.approx(2.6 * 1.1)
    assert row["away_goals"] == pytest.approx(2.3 * 0.9 * 0.9)


def _base(game_ids: list[int]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "game_id": game_ids,
            "season": [20132014] * len(game_ids),
            "game_date": [date(2013, 11, 1)] * len(game_ids),
            "start_utc": [PREDICTION + timedelta(hours=9)] * len(game_ids),
            "prediction_utc": [PREDICTION] * len(game_ids),
            "rested": [True] * len(game_ids),
        }
    ).with_columns(pl.col("season").cast(pl.Int32))


def test_the_over_is_settled_on_the_final_score_and_a_push_is_void() -> None:
    quotes = [*game_quotes(1), *game_quotes(2, close_line=5.0), *game_quotes(3)]
    totals = th.market(sbr(quotes), moments([1, 2, 3]))
    found = pl.DataFrame({"game_id": [1, 2, 3], "total": [5.0, 5.0, 5.0]})
    games = pl.DataFrame(
        {
            "game_id": [1, 2, 3],
            "home_score": [4, 3, 2],
            "away_score": [3, 2, 3],
            "observed_utc": [PREDICTION + timedelta(days=1)] * 3,
        }
    )
    frame = th.rows(_base([1, 2, 3]), totals, found, games)
    assert frame["over"].to_list() == [1, None, 0]
    assert frame["goals"].to_list() == [7, 5, 5]


def test_a_projection_that_knows_the_move_scores_a_negative_move() -> None:
    frame = scored_frame(signal=0.3)
    games = th.scored(frame, ("total",), (20132014, 20142015))
    assert games["season"].unique().sort().to_list() == [20132014, 20142015]
    assert games["move"].mean() < 0
    assert games["result"].mean() < 0
    # A push has no result, but a move.
    pushed = games.filter(pl.col("over").is_null())
    assert pushed.height > 0
    assert pushed["result"].null_count() == pushed.height
    assert pushed["move"].null_count() == 0


def test_a_projection_without_signal_has_no_move_skill() -> None:
    frame = scored_frame(signal=0.0)
    found = th.figures(th.scored(frame, ("total",), (20132014, 20142015)))
    assert found["move"]["high"] > 0


def test_the_first_season_fitted_on_is_never_scored() -> None:
    frame = scored_frame()
    games = th.scored(frame, ("total",), (20122013, 20132014))
    assert games["season"].unique().to_list() == [20132014]


def test_figures_have_every_measure_overall_rested_and_per_season() -> None:
    frame = scored_frame()
    found = th.figures(th.scored(frame, ("total",), (20132014, 20142015)))
    for measure in th.MEASURES:
        for key in (measure, f"{measure}_rested"):
            assert {"mean", "low", "high", "p_value"} <= set(found[key])
        assert set(found[f"{measure}_per_season"]) == {"20132014", "20142015"}
    assert found["pushes"] > 0


def test_run_scores_both_candidates_and_describes_the_games() -> None:
    frame = scored_frame()
    per_game, found = th.run(frame, "explore")
    assert set(per_game["candidate"].unique().to_list()) == set(th.CANDIDATES)
    assert set(found) == {*th.CANDIDATES, "described"}
    assert (
        found["described"]["games"]
        == frame.filter(pl.col("season").is_in([20132014, 20142015])).height
    )
    markdown = th.markdown(found, "explore", "totals-history-test")
    assert "## Supported on the explore half" in markdown


def test_supported_pairs_are_those_with_an_interval_below_zero() -> None:
    below = {"mean": -1.0, "low": -2.0, "high": -0.5}
    across = {"mean": -1.0, "low": -2.0, "high": 0.5}
    found = {
        "total": {"move": below, "result": across},
        "total split": {"move": across, "result": across},
    }
    assert th.supported(found) == [("total", "move")]


def test_a_confirm_verdict_needs_a_claim_and_applies_holm() -> None:
    with pytest.raises(ValueError, match="no claim"):
        th.verdict({}, ())
    found = {
        "total": {
            "move": {"mean": -1.0, "low": -2.0, "high": -0.5, "p_value": 0.01},
            "result": {"mean": -1.0, "low": -2.0, "high": 0.5, "p_value": 0.04},
        }
    }
    tests = th.verdict(found, (("total", "move"), ("total", "result")))
    assert [t["holds"] for t in tests] == [True, True]
    tests = th.verdict(
        {"total": {**found["total"], "result": {**found["total"]["result"], "p_value": 0.03}}},
        (("total", "move"), ("total", "result")),
    )
    assert [t["passes"] for t in tests] == [True, True]
    late = {"total": {**found["total"], "move": {**found["total"]["move"], "p_value": 0.2}}}
    tests = th.verdict(late, (("total", "move"), ("total", "result")))
    assert [t["holds"] for t in tests] == [False, False]
