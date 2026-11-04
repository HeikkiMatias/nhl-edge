"""scripts/lineup_change.py (#370): the lineup change by the rule fixed on #370, and its
decision under Holm's step-down over the family, on made-up tables."""

from datetime import timedelta

import polars as pl
import pytest
from lineup_change_fixtures import D, boxscores, feature, load, projected

lc = load()


def test_the_change_is_tonights_strength_less_the_last_games_home_less_away() -> None:
    # BOS: tonight 101 (0.2) and 105 (unrated, 0) at 10 minutes each, so 5 x 0.1 = 0.5; last
    # game 101 and 102 (0.4) at equal time, 5 x 0.3 = 1.5. NYR's lineup is unchanged.
    assert feature() == pytest.approx(0.5 - 1.5)


def test_goalies_and_the_power_play_never_count() -> None:
    # The fixture's goalie dressed in BOS's last game, and every player has a pp rating of 9.
    no_goalie = boxscores().filter(pl.col("role") != "G")
    assert feature(actual_lineups=no_goalie) == pytest.approx(feature())


def test_a_teams_first_game_of_the_season_has_no_change() -> None:
    nyr_only = boxscores().filter(pl.col("team") == "NYR")
    # BOS has no earlier game: its change is 0, and NYR's is 0 too.
    assert feature(actual_lineups=nyr_only) == 0.0


def test_weights_are_tonights_minutes_and_the_last_games_time_on_ice() -> None:
    heavier = projected(
        [
            ("BOS", 101, "F", 30.0),
            ("BOS", 105, "D", 10.0),
            ("NYR", 201, "F", 10.0),
            ("NYR", 202, "D", 10.0),
        ]
    )
    # BOS tonight: 5 x (0.2 x 30) / 40 = 0.75.
    assert feature(lineups=heavier) == pytest.approx(0.75 - 1.5)
    longer = boxscores(
        [
            (1, D, "BOS", 101, "F", 3000),
            (1, D, "BOS", 102, "D", 1000),
            (2, D + timedelta(days=1), "NYR", 201, "F", 1000),
            (2, D + timedelta(days=1), "NYR", 202, "D", 1000),
        ]
    )
    # BOS's last game: 5 x (0.2 x 3000 + 0.4 x 1000) / 4000 = 1.25.
    assert feature(actual_lineups=longer) == pytest.approx(0.5 - 1.25)


def test_it_is_kept_only_when_it_passes_holm_with_a_negative_estimate() -> None:
    others = {"travel": 0.788, "density": 0.0, "early": 0.0, "goalie_presumed": 0.194}
    per_game = pl.DataFrame(
        {
            "candidate": ["B3 split + lineup_change", "B3 split"] * 3,
            "game_id": [1, 1, 2, 2, 3, 3],
            "season": [20132014] * 6,
            "game_date": [D, D, D + timedelta(days=7), D + timedelta(days=7)] + [D] * 2,
            "move": [0.0] * 6,
        }
    )

    def found(mean: float, p: float) -> dict[str, dict[str, object]]:
        cell = {"mean": mean, "low": mean, "high": mean, "games": 3, "weeks": 2}
        return {lc.NAME: {"move": cell, "move_p": p, "move_rested": cell, "result": cell}}

    assert lc.decide(per_game, found(-0.001, 0.001), others)["kept"]
    # A p-value above its step's threshold, or a positive estimate, is not kept.
    assert not lc.decide(per_game, found(-0.001, 0.2), others)["kept"]
    assert not lc.decide(per_game, found(0.001, 0.001), others)["kept"]
    assert set(lc.decide(per_game, found(-0.001, 0.001), others)["holm"]) == set(lc.FAMILY)


def test_the_earlier_four_are_read_from_389s_record() -> None:
    found = lc.earlier()
    assert set(found) == set(lc.FAMILY) - {lc.NAME}
    assert found["density"] == 0.0 and found["travel"] == pytest.approx(0.788)
