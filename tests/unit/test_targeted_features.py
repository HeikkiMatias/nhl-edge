"""scripts/targeted_features.py (#370): the density, early-season and presumed-starter columns,
and the family's decision, on made-up games."""

from datetime import date, timedelta

import polars as pl
import pytest
from targeted_features_fixtures import load, moments, schedule

tf = load()
D = date(2013, 11, 1)


def day(n: int) -> date:
    return D + timedelta(days=n)


# BOS plays at NYR, then at PHI the next night, then at home; NYR rests between its games.
GAMES = schedule(
    [
        (1, day(0), "NYR", "BOS"),
        (2, day(1), "PHI", "BOS"),
        (3, day(3), "BOS", "NYR"),
        (4, day(4), "BOS", "PHI"),
    ]
)


def test_density_counts_earlier_dates_and_road_games_in_a_row() -> None:
    found = {r["game_id"]: r for r in tf.density(GAMES, moments(GAMES)).iter_rows(named=True)}
    # Game 3 on day 3: BOS played on days 0 and 1, NYR on day 0, all within the 3 days before.
    assert found[3]["dense3_diff"] == 2 - 1 and found[3]["dense6_diff"] == 2 - 1
    # BOS comes home from 2 road games; NYR's one road game is tonight's, none before.
    assert found[3]["home_return"] == 2 and found[3]["away_trip"] == 0
    # Game 2: BOS on a back-to-back at PHI, who has not played yet (rested).
    assert found[2]["mismatch"] == 1.0
    # Game 4: BOS on a back-to-back at home against PHI, rested 3 days: the other way.
    assert found[4]["mismatch"] == -1.0
    # A team's first game counts nothing before it.
    assert found[1]["dense3_diff"] == 0 and found[1]["away_trip"] == 0


def test_early_keeps_the_5v5_part_only_when_both_teams_are_in_their_first_games() -> None:
    rows = [(i, day(2 * i), "NYR", "BOS") for i in range(1, 23)]
    games = schedule(rows)
    frame = moments(games).with_columns(five_diff=pl.lit(0.3))
    found = tf.early(frame, games).sort("game_id")["five_diff_early"].to_list()
    assert found[: tf.EARLY] == [0.3] * tf.EARLY and found[tf.EARLY :] == [0.0, 0.0]


def test_presumed_is_the_likeliest_pair_less_the_mixture() -> None:
    pairs = pl.DataFrame(
        {
            "game_id": [1, 1, 1, 2],
            "weight": [0.6, 0.3, 0.1, 1.0],
            "goalie_diff": [0.10, -0.05, 0.20, 0.07],
        }
    )
    found = dict(tf.presumed(pairs).iter_rows())
    mixture = 0.6 * 0.10 + 0.3 * -0.05 + 0.1 * 0.20
    assert found[1] == pytest.approx(0.10 - mixture)
    # One scenario: the likeliest is the mixture.
    assert found[2] == pytest.approx(0.0)
    # Tied likeliest pairs are averaged.
    tied = pairs.with_columns(weight=pl.Series([0.45, 0.45, 0.1, 1.0]))
    mixture = 0.45 * 0.10 + 0.45 * -0.05 + 0.1 * 0.20
    assert dict(tf.presumed(tied).iter_rows())[1] == pytest.approx(0.025 - mixture)


def _per_game(candidate: str, moves: list[float], rested: bool = True) -> pl.DataFrame:
    n = len(moves)
    return pl.DataFrame(
        {
            "candidate": [candidate] * n,
            "game_id": list(range(n)),
            "season": [20132014] * n,
            "game_date": [day(7 * (i % 8)) for i in range(n)],
            "rested": [rested] * n,
            "move": moves,
        }
    ).with_columns(pl.col("season").cast(pl.Int32))


def test_the_decision_is_holm_over_five_with_the_rested_rule_for_schedule_facts() -> None:
    n = 160
    per_game = pl.concat(
        [
            _per_game("B3 split", [0.0] * n),
            _per_game("early", [-0.002] * n),
            _per_game("B3 split + goalie_presumed", [0.001] * n),
        ]
    )
    estimate = {"mean": -0.002, "low": -0.003, "high": -0.001}
    found = {
        "travel": {"move": estimate, "move_p": 0.0001, "move_rested": estimate},
        "density": {
            "move": estimate,
            "move_p": 0.0001,
            "move_rested": {"mean": -0.001, "low": -0.003, "high": 0.001},
        },
    }
    decision = tf.decide(per_game, found)["estimates"]
    assert decision["travel"]["kept"]
    # Density passes Holm but not on the rested games: not kept.
    assert not decision["density"]["kept"]
    assert decision["early"]["kept"] and decision["early"]["primary"]["mean"] == pytest.approx(
        -0.002
    )
    assert not decision["goalie_presumed"]["kept"]
    # Five in the family: the smallest p's threshold is 0.05 / 5.
    assert min(e["threshold"] for e in decision.values()) == pytest.approx(0.01)
