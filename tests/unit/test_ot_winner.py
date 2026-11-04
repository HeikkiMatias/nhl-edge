"""scripts/ot_winner.py (#379 step 1): ties from regulation goals, the shrunk home rate, the MAP
fit, and the gate, on made-up games."""

from datetime import date

import numpy as np
import polars as pl
import pytest
from ot_winner_fixtures import league, load

ow = load()


def games_and_shots() -> tuple[pl.DataFrame, pl.DataFrame]:
    games = pl.DataFrame(
        {
            "game_id": [1, 2, 3, 4],
            "season": [20162017] * 4,
            "game_date": [date(2016, 11, 1)] * 4,
            "home_score": [3, 2, 4, 3],
            "away_score": [2, 3, 1, 2],
            # 1: a shootout won at home; 2: overtime won away; 3: regulation; 4: OT, but the
            # goals say regulation ended 3-2, so it is dropped.
            "decided_in": ["SO", "OT", "REG", "OT"],
        }
    ).with_columns(pl.col("season").cast(pl.Int32))
    goals = [(1, True), (1, True), (1, False), (1, False), (2, True), (2, True), (2, False)]
    goals += [(2, False), (3, True), (3, True), (3, True), (3, True), (3, False)]
    goals += [(4, True), (4, True), (4, True), (4, False), (4, False)]
    shots = pl.DataFrame(
        {
            "game_id": [g for g, _ in goals],
            "is_home": [h for _, h in goals],
            "is_goal": [True] * len(goals),
            "period": [2] * len(goals),
        }
    )
    return games, shots


def test_ties_come_from_regulation_goals_and_the_label_from_the_winner() -> None:
    games, shots = games_and_shots()
    tied, disagree = ow.ties(games, shots)
    assert tied.sort("game_id")["game_id"].to_list() == [1, 2]
    assert dict(tied.select("game_id", "home_won").iter_rows()) == {1: 1, 2: 0}
    assert disagree == 1


def test_the_home_rate_is_shrunk_toward_a_half() -> None:
    train = pl.DataFrame({"home_won": [1] * 60 + [0] * 40})
    assert ow.shrunk_rate(train) == pytest.approx((60 + 150) / (100 + 300))


def test_the_map_fit_recovers_a_strong_slope_and_shrinks_a_weak_sample() -> None:
    rng = np.random.default_rng(2)
    x = rng.normal(0, 0.4, 20000)
    y = (rng.random(20000) < 1 / (1 + np.exp(-(0.1 + 0.8 * x)))).astype(float)
    a, c = ow.map_logistic(x, y, 0.5)
    assert a == pytest.approx(0.1, abs=0.05) and c == pytest.approx(0.8, abs=0.1)
    _, small = ow.map_logistic(x[:40], y[:40], 0.5)
    assert abs(small) < 0.8


def test_no_strength_effect_fails_the_gate_and_a_strong_one_passes_it() -> None:
    flat = ow.run(*league(0.0))
    assert not flat["gate"]
    assert set(flat["against_q0"]) == {"q1", "q2", "q3"}
    strong = ow.run(*league(1.5))
    assert strong["gate"] and strong["gate_by"]["q2"]
    assert strong["against_q1"]["q2"]["log_loss"]["high"] < 0
    # q3 is scored on the training seasons only.
    assert strong["fits"]["20212022"].get("q3") is None
    assert "Step 2 goes ahead" in ow.markdown(strong, 0, "ot-winner-20261104-abc1234")
    assert "does not go ahead" in ow.markdown(flat, 0, "ot-winner-20261104-abc1234")


def test_the_regulation_label_never_reads_the_final_score() -> None:
    # A shootout's extra goal makes the final 3-2; the regulation goals, 2-2, make it a tie.
    games, shots = games_and_shots()
    tied, _ = ow.ties(games.filter(pl.col("game_id") == 1), shots)
    assert tied.height == 1


def test_the_shift_share_has_an_interval_and_the_gate_reads_its_low_end() -> None:
    found = ow.run(*league(1.5))
    share = found["shifts"]["q2"]["share"]
    assert share["low"] <= share["mean"] <= share["high"] and share["weeks"] > 0
    # A share whose low end is under SHARE never passes, whatever its point estimate.
    assert found["gate_by"]["q2"] == (
        found["gate_holm"]["q2"]["passes"]
        and found["against_q1"]["q2"]["log_loss"]["high"] < 0
        and share["low"] >= ow.SHARE
    )
