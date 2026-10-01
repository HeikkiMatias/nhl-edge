from datetime import UTC, datetime

import market_history
import numpy as np
import polars as pl
import pytest
from b2_fixtures import G_WEIGHT, P_A, S_WEIGHT, SAVED, feature_tables, goalie_id, league
from scipy.optimize import check_grad

from nhl_edge.backtest import b2_report, reports
from nhl_edge.backtest.metrics import calibration
from nhl_edge.backtest.walk_forward import fold_start, run
from nhl_edge.game import b2

LEAGUE = league()
TEST = 20182019
START = fold_start(LEAGUE.games.select("season", "start_utc"), TEST)


def test_the_objectives_gradient_is_right() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(50, 4))
    y = (rng.random(50) < 0.5).astype(float)
    offset = rng.normal(size=50)
    loss = b2.objective(x, y, offset, 3.0)
    beta = rng.normal(size=5)
    assert check_grad(lambda b: loss(b)[0], lambda b: loss(b)[1], beta) < 1e-5


def test_scenarios_pair_every_candidate_and_weigh_them_by_both_probabilities() -> None:
    games = pl.DataFrame({"game_id": [1, 2], "home": ["BOS", "BOS"], "away": ["TOR", "MTL"]})
    pool = pl.DataFrame(
        {
            "game_id": [1, 1, 1, 1, 2, 2],
            "team": ["BOS", "BOS", "TOR", "TOR", "BOS", "BOS"],
            "p_start": [0.7, 0.3, 0.6, 0.4, 0.7, 0.3],
            "goals_saved": [0.4, -0.4, 0.2, -0.2, 0.4, -0.4],
        }
    )
    pairs = b2.scenarios(games, pool)
    first = pairs.filter(pl.col("game_id") == 1)
    assert first.height == 4 and first["weight"].sum() == pytest.approx(1.0)
    assert sorted(first["delta_g"].round(6).to_list()) == [-0.6, -0.2, 0.2, 0.6]
    # MTL has no candidates: one average goalie.
    second = pairs.filter(pl.col("game_id") == 2).sort("delta_g")
    assert second["delta_g"].to_list() == pytest.approx([-0.4, 0.4])
    assert second["weight"].to_list() == pytest.approx([0.3, 0.7])


def test_the_prediction_averages_over_the_pairs() -> None:
    model = b2.B2Model(
        season=TEST,
        settings=b2.Settings(1.0),
        intercept=0.1,
        weights=tuple(1.0 if name == "delta_g" else 0.0 for name in b2.INPUTS),
        means=tuple(0.0 for _ in b2.INPUTS),
        scales=tuple(1.0 for _ in b2.INPUTS),
        games=1,
        train_cutoff=datetime(2018, 4, 9, tzinfo=UTC),
    )
    inputs = pl.DataFrame(
        {"game_id": [1], "offset": [0.2], **{n: [0.0] for n in b2.INPUTS if n != "delta_g"}}
    )
    pairs = pl.DataFrame({"game_id": [1, 1], "weight": [0.25, 0.75], "delta_g": [1.0, -1.0]})
    expected = 0.25 / (1 + np.exp(-(0.3 + 1.0))) + 0.75 / (1 + np.exp(-(0.3 - 1.0)))
    assert model.predict(inputs, pairs)["p_home"].item() == pytest.approx(expected)


def test_training_uses_the_goalies_who_started() -> None:
    delta = (
        b2.starters_delta(LEAGUE, START)
        .join(LEAGUE.games.select("game_id", "home", "away"), on="game_id")
        .sort("game_id")
    )
    starters = LEAGUE.actual_lineups.filter("starting_goalie").select(
        "game_id", "team", "player_id"
    )
    row = delta.row(0, named=True)
    home = starters.filter(pl.col("game_id") == row["game_id"], pl.col("team") == row["home"])
    away = starters.filter(pl.col("game_id") == row["game_id"], pl.col("team") == row["away"])

    def saved(goalie: int, team: str) -> float:
        return SAVED["A"] if goalie == goalie_id(team, "A") else SAVED["B"]

    expected = saved(home["player_id"].item(), row["home"]) - saved(
        away["player_id"].item(), row["away"]
    )
    assert row["delta_g"] == pytest.approx(expected)
    # A starter without a goalie_effects row counts as average.
    missing = b2.Tables(
        LEAGUE.games,
        LEAGUE.team_strength,
        LEAGUE.schedule_terms,
        LEAGUE.goalie_starts,
        LEAGUE.goalie_effects.filter(pl.col("game_id") != row["game_id"]),
        LEAGUE.actual_lineups,
    )
    assert (
        b2.starters_delta(missing, START)
        .filter(pl.col("game_id") == row["game_id"])["delta_g"]
        .item()
        == 0.0
    )


def test_the_fit_recovers_the_known_weights() -> None:
    inputs = b2.game_inputs(LEAGUE)
    train = b2.training_games(LEAGUE, inputs, TEST, START, "observed_utc")
    assert set(train["season"]) == {20162017, 20172018}
    assert (train["result_utc"] < START).all() and (train["observed_utc"] < START).all()
    model = b2.fit(train, b2.Settings(0.1), TEST)
    weights = dict(zip(b2.INPUTS, model.weights, strict=True))
    scales = dict(zip(b2.INPUTS, model.scales, strict=True))
    # The weights are on standardized inputs: per unit, they are close to the truth.
    assert weights["delta_s"] / scales["delta_s"] == pytest.approx(S_WEIGHT, rel=0.3)
    assert weights["delta_g"] / scales["delta_g"] == pytest.approx(G_WEIGHT, rel=0.4)
    # Inputs that do not move the result stay near 0: within about three standard errors on 800
    # games.
    for name in ("home_back_to_back", "away_back_to_back", "rest_diff", "travel_diff"):
        assert abs(weights[name]) < 0.25
    # h_s is an offset, so β0 absorbs only what it misses: nothing here.
    assert abs(model.intercept) < 0.15


def test_predictions_mix_over_starters_and_carry_the_cutoff() -> None:
    moments = LEAGUE.games.filter(pl.col("season") == TEST).select(
        "game_id", prediction_utc="start_utc"
    )
    predicted, model = b2.predictions(LEAGUE, moments, TEST, START, b2.TUNED)
    assert predicted.height == moments.height
    assert (predicted["train_cutoff"] == model.train_cutoff).all() and model.train_cutoff < START
    assert predicted["p_home"].is_between(0, 1, closed="none").all()
    # The more likely good goalie for the home side makes it likelier to win.
    assert model.weights[b2.GOALIE] > 0
    assert 0 < P_A < 1


def test_a_fold_before_the_tuning_cutoff_is_refused() -> None:
    early = league((20152016, 20162017), games=40)
    start = fold_start(early.games.select("season", "start_utc"), 20162017)
    moments = early.games.filter(pl.col("season") == 20162017).select(
        "game_id", prediction_utc="start_utc"
    )
    with pytest.raises(ValueError, match="before the tuning cutoff"):
        b2.predictions(early, moments, 20162017, start, b2.TUNED)


def test_input_problems() -> None:
    counts = dict(LEAGUE.games.group_by("season").len().iter_rows())
    assert b2.input_problems(LEAGUE, TEST, counts) == []
    first = LEAGUE.games["game_id"].min()
    gap = b2.Tables(
        LEAGUE.games,
        LEAGUE.team_strength.filter(pl.col("game_id") != first),
        LEAGUE.schedule_terms,
        LEAGUE.goalie_starts,
        LEAGUE.goalie_effects,
        LEAGUE.actual_lineups,
    )
    assert b2.input_problems(gap, TEST, counts) == [
        f"20162017: 1 games without team_strength, e.g. {first}"
    ]


def test_the_grid_and_the_tuned_setting() -> None:
    assert b2.TUNED in b2.GRID and max(b2.GRID, key=b2.steadiness).l2 == 1000


def test_calibration_of_calibrated_and_overconfident_probabilities() -> None:
    rng = np.random.default_rng(1)
    days = [datetime(2018, 10, 1, tzinfo=UTC).date()]
    n = 4000
    p = rng.uniform(0.2, 0.8, n)
    frame = pl.DataFrame(
        {
            "season": [TEST] * n,
            "game_date": [days[0] + np.timedelta64(int(i // 20), "D").item() for i in range(n)],
            "p": p,
            "y": (rng.random(n) < p).astype(int),
        }
    )
    fitted = calibration(frame, "p", "y", draws=200)
    # Close to 0 and 1, each inside its own interval (a fixed tolerance, not a test at 95%).
    assert fitted["slope"].value == pytest.approx(1.0, abs=0.15)
    assert fitted["intercept"].value == pytest.approx(0.0, abs=0.1)
    for name in ("slope", "intercept"):
        assert fitted[name].low < fitted[name].value < fitted[name].high
    # Doubling the log-odds makes the probabilities overconfident: the slope halves.
    logit = np.log(p / (1 - p))
    sharp = frame.with_columns(p=pl.Series(1 / (1 + np.exp(-2 * logit))))
    assert calibration(sharp, "p", "y", draws=200)["slope"].value == pytest.approx(0.5, abs=0.1)


def test_the_walk_forward_scores_b2_beside_b1() -> None:
    odds, games = market_history.seasons([20172018, TEST], games=40)
    tables = feature_tables(games)
    fits: dict[str, dict[int, b2.B2Model]] = {}
    predictions, coverage, b1_fits = run(odds, games, [TEST], b2_tables=tables, b2_fits=fits)
    for experiment in ("E1", "E2"):
        rows = predictions.filter(pl.col("experiment") == experiment)
        b1 = rows.filter(pl.col("model") == "B1")
        b2_rows = rows.filter(pl.col("model") == "B2")
        assert sorted(b2_rows["game_id"]) == sorted(b1["game_id"])
        assert set(b2_rows["method"]) == {"none"}
        counts = coverage[experiment][TEST]
        assert counts["b2_scored"] == b2_rows.height == counts["scored"]
        assert counts["b2_trained_on"] == fits[experiment][TEST].games == 40
        assert (b2_rows["train_cutoff"] < b2_rows["prediction_utc"]).all()
    report = reports.summary(
        predictions, coverage, b1_fits, [TEST], "backtest-x", datetime.now(UTC)
    )
    report = b2_report.add(
        report, predictions, fits, games, tables.goalie_starts, tables.actual_lineups, [TEST]
    )
    model = report["experiments"]["E1"]["models"]["B2"]
    assert set(model) >= {
        "log_loss",
        "paired_against_B1",
        "fits",
        "calibration",
        "gaps_over_8_points",
    }
    assert model["fits"][str(TEST)]["l2"] == b2.TUNED.l2
    assert set(model["calibration"]["pooled"]) == {"intercept", "slope"}
    gaps = model["gaps_over_8_points"]
    assert gaps["games_compared"] == 40 and gaps["count"] == len(gaps["games"])
    assert all(abs(g["gap"]) > 0.08 and "home_win" not in g for g in gaps["games"])
    quality = report["lineup_quality"]["goalie_starts"]
    # Two candidates at 0.7 and 0.3: the Brier score is 2 * 0.3 ** 2 when A starts, 2 * 0.7 ** 2
    # when B does.
    expected = P_A * 2 * 0.3**2 + (1 - P_A) * 2 * 0.7**2
    assert quality["pooled"]["brier"]["mean"] == pytest.approx(expected, abs=0.08)
    assert quality["pooled"]["missed"] == 0.0
