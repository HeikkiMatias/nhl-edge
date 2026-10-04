import polars as pl
import pytest
from uncertainty_fixtures import GOALIES, MOMENTS, tables

from nhl_edge.game import uncertainty as un


def test_each_part_by_hand() -> None:
    row = un.parts(tables(), MOMENTS).row(0, named=True)
    # BOS's likeliest starter is 60% (doubt 0.4), TOR's is certain (0).
    assert row["goalie_doubt"] == pytest.approx(0.2)
    # BOS: one skater at 50% (0.25); TOR: one at 90% (0.09).
    assert row["availability_doubt"] == pytest.approx(0.17)
    # Of 80 projected 5v5 minutes, 102 (58 NHL games last season and 12 this one, 70 < 82) has
    # 20, 103 (no NHL games) 5, and TOR's replacement slots 10.
    assert row["rookie_share"] == pytest.approx(35 / 80)


def test_a_team_without_candidate_goalies_is_fully_in_doubt() -> None:
    no_tor = GOALIES.filter(pl.col("team") != "TOR")
    row = un.parts(tables(goalie_starts=no_tor), MOMENTS).row(0, named=True)
    assert row["goalie_doubt"] == pytest.approx((0.4 + 1.0) / 2)


def test_earlier_games_add_nhl_regular_season_lines_and_this_seasons_boxscores() -> None:
    skaters = pl.DataFrame(
        {"game_id": [2021020200] * 3, "season": [20212022] * 3, "player_id": [101, 102, 103]}
    ).with_columns(prediction_utc=MOMENTS["prediction_utc"][0])
    counts = dict(
        un.earlier_games(skaters, tables()).select("player_id", "earlier_games").iter_rows()
    )
    assert counts == {101: 300, 102: 70, 103: 0}


def test_u_averages_the_standardized_parts() -> None:
    training = pl.DataFrame(
        {
            "goalie_doubt": [0.1, 0.3],
            "availability_doubt": [1.0, 3.0],
            "rookie_share": [0.2, 0.2],
        }
    )
    scale = un.fit_scale(training)
    # A part that never varies gets a spread of 1, so it adds nothing beyond its mean.
    assert scale.sds[2] == 1.0
    assert scale.games == 2
    u = scale.score(training)
    assert u.to_list() == pytest.approx([-2 / 3, 2 / 3])
    with pytest.raises(ValueError, match="no training games"):
        un.fit_scale(training.clear())
