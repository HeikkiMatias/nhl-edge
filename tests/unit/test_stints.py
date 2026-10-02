import polars as pl
import pytest
from feed_fixtures import MTL_ARI, OPENING_WEEK_GAMES, parsed_feeds
from stint_fixtures import (
    AWAY_GOALIE,
    HOME_GOALIES,
    PERIOD_END,
    SEASON,
    XG_CUTOFF,
    XG_VERSION,
    build,
    coverage_frame,
    empty_shot_xg,
    full_period,
    real_calendar,
    without,
)

from nhl_edge.features import stints

FIXTURE_GAMES = (*OPENING_WEEK_GAMES, MTL_ARI)


def fixture_stints(game_id: int) -> tuple[pl.DataFrame, dict[str, pl.DataFrame]]:
    tables = parsed_feeds(game_id)
    frame = stints.build(
        tables["shift_coverage"],
        tables["shifts"],
        tables["actual_lineups"],
        tables["shots"],
        empty_shot_xg(),
        tables["faceoffs"],
        real_calendar([game_id]),
    )
    return frame, tables


@pytest.mark.parametrize("game_id", FIXTURE_GAMES)
def test_a_real_games_stints_fill_each_period_end_to_end(game_id: int) -> None:
    frame, tables = fixture_stints(game_id)
    length = tables["strength_time"]["game_seconds"][0]
    assert frame["seconds"].sum() == length
    gaps = frame.with_columns(prev_end=pl.col("end_s").shift(1).over("period")).filter(
        pl.col("prev_end").is_not_null() & (pl.col("prev_end") != pl.col("start_s"))
    )
    assert gaps.is_empty()
    assert frame["stint_id"].to_list() == list(range(1, frame.height + 1))


@pytest.mark.parametrize("game_id", FIXTURE_GAMES)
def test_a_real_games_goals_land_in_its_stints_and_set_the_score(game_id: int) -> None:
    frame, tables = fixture_stints(game_id)
    goals = tables["shots"].filter(pl.col("is_goal"))
    in_play = goals.filter(~pl.col("is_penalty_shot"))
    assert frame["home_goals"].sum() == in_play["is_home"].sum()
    assert frame["away_goals"].sum() == (~in_play["is_home"]).sum()
    sign = pl.when(pl.col("is_home")).then(1).otherwise(-1)
    for row in frame.select("start_s", "score_state").iter_rows(named=True):
        before = goals.filter(pl.col("seconds") <= row["start_s"]).select(sign.sum()).item()
        assert row["score_state"] == before


def test_mtl_ari_keeps_the_penalty_shot_goal_out_of_its_stint_but_in_the_score() -> None:
    frame, tables = fixture_stints(MTL_ARI)
    penalty_goal = tables["shots"].filter(pl.col("is_penalty_shot") & pl.col("is_goal"))
    assert penalty_goal.height == 1
    seconds = penalty_goal["seconds"][0]
    after = frame.filter(pl.col("start_s") >= seconds)["score_state"][0]
    before = frame.filter(pl.col("end_s") <= seconds)["score_state"][-1]
    assert after == before + 1


def test_one_group_on_the_ice_all_period_is_one_stint() -> None:
    frame = build(full_period())
    assert frame.height == 1
    row = frame.row(0, named=True)
    assert (row["start_s"], row["end_s"], row["seconds"]) == (0, PERIOD_END, PERIOD_END)
    assert row["home_skaters"] == [1, 2, 3, 4, 5]
    assert row["away_skaters"] == [11, 12, 13, 14, 15]
    assert (row["home_goalie"], row["away_goalie"]) == (HOME_GOALIES[0], AWAY_GOALIE)
    assert row["strength"] == "5v5"
    assert row["drop_reason"] is None


def test_a_change_cuts_a_stint_and_a_split_shift_does_not() -> None:
    shifts = [*without(full_period(), 5), (5, 0, 600), (6, 600, PERIOD_END)]
    # Player 1's shift is recorded in two pieces, with nothing else changing at 300.
    shifts = [*without(shifts, 1), (1, 0, 300), (1, 300, PERIOD_END)]
    frame = build(shifts)
    assert frame.select("start_s", "end_s").rows() == [(0, 600), (600, PERIOD_END)]
    assert frame["home_skaters"].to_list() == [[1, 2, 3, 4, 5], [1, 2, 3, 4, 6]]


def test_a_goal_at_a_change_belongs_to_the_stint_it_ends() -> None:
    shifts = [*without(full_period(), 5), (5, 0, 600), (6, 600, PERIOD_END)]
    frame = build(shifts, shots=[(1, 600, True, True), (2, 700, False, False)])
    first, second = frame.iter_rows(named=True)
    assert (first["home_goals"], first["score_state"]) == (1, 0)
    assert (second["home_goals"], second["score_state"]) == (0, 1)


def test_a_penalty_gives_a_short_handed_stint() -> None:
    shifts = [*without(full_period(), 5), (5, 0, 400), (5, 520, PERIOD_END)]
    frame = build(shifts)
    assert frame.select("start_s", "strength").rows() == [(0, "5v5"), (400, "4v5"), (520, "5v5")]
    assert frame["drop_reason"].null_count() == 3


def test_the_zone_comes_from_a_faceoff_at_the_stints_start_only() -> None:
    shifts = [*without(full_period(), 5), (5, 0, 600), (6, 600, PERIOD_END)]
    frame = build(shifts, faceoffs=[(0, "N"), (600, "O"), (650, "D")])
    assert frame["zone_start"].to_list() == ["N", "O"]
    on_the_fly = build(shifts, faceoffs=[(0, "N")])
    assert on_the_fly["zone_start"].to_list() == ["N", None]


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        # Two home skaters off at once with no one coming on: 3 left is possible, 2 is not.
        ([(3, 0, 400), (3, 500, PERIOD_END), (4, 0, 400), (4, 500, PERIOD_END)], None),
        (
            [
                (2, 0, 400),
                (2, 500, PERIOD_END),
                (3, 0, 400),
                (3, 500, PERIOD_END),
                (4, 0, 400),
                (4, 500, PERIOD_END),
            ],
            "skaters",
        ),
        ([(6, 400, 500), (7, 400, 500)], "skaters"),
        ([(HOME_GOALIES[1], 400, 500)], "goalies"),
    ],
)
def test_only_impossible_counts_are_left_out_and_they_stay_in_the_table(
    extra: list[tuple[int, int, int]], reason: str | None
) -> None:
    replaced = {player for player, _, _ in extra}
    shifts = [s for s in full_period() if s[0] not in replaced] + extra
    frame = build(shifts)
    middle = frame.filter(pl.col("start_s") == 400).row(0, named=True)
    assert middle["drop_reason"] == reason
    assert frame.filter(pl.col("start_s") != 400)["drop_reason"].null_count() == 2
    if reason == "goalies":
        assert middle["home_goalie"] is None


def test_a_pulled_goalie_is_kept_with_six_skaters() -> None:
    goalie = HOME_GOALIES[0]
    shifts = [*without(full_period(), goalie), (goalie, 0, 1150), (6, 1150, PERIOD_END)]
    frame = build(shifts)
    pulled = frame.filter(pl.col("start_s") == 1150).row(0, named=True)
    assert pulled["strength"] == "6v5"
    assert pulled["home_goalie"] is None
    assert pulled["drop_reason"] is None


def test_xg_adds_up_per_team_and_carries_its_model() -> None:
    shots = [
        (1, 100, True, False),
        (2, 200, False, False),
        (3, 250, True, False),
        (4, 400, True, False),
    ]
    # Shot 4 has no coordinates, so the xG model gives it none, as it gives an empty-net shot.
    frame = build(full_period(), shots=shots, xg={1: 0.1, 2: 0.2, 3: 0.3}, no_coordinates=(4,))
    row = frame.row(0, named=True)
    assert row["home_xg"] == pytest.approx(0.4)
    assert row["away_xg"] == pytest.approx(0.2)
    assert (row["xg_version"], row["xg_train_cutoff"]) == (XG_VERSION, XG_CUTOFF)


def test_a_shot_the_xg_model_scores_without_xg_is_refused() -> None:
    shots = [(1, 100, True, False), (2, 200, False, False)]
    with pytest.raises(ValueError, match="1 shots of 1 games have no xG"):
        build(full_period(), shots=shots, xg={1: 0.1})


def test_a_goal_cuts_the_stint_even_when_no_one_changes() -> None:
    shots = [(1, 300, True, True), (2, 500, False, False)]
    frame = build(full_period(), shots=shots, faceoffs=[(0, "N"), (300, "N")])
    assert frame.select("start_s", "end_s", "score_state", "home_goals", "zone_start").rows() == [
        (0, 300, 0, 1, "N"),
        (300, PERIOD_END, 1, 0, "N"),
    ]
    assert frame["home_skaters"].to_list() == [[1, 2, 3, 4, 5]] * 2


def test_a_season_with_xg_from_two_models_is_refused() -> None:
    from stint_fixtures import (
        calendar_frame,
        faceoffs_frame,
        lineups_frame,
        shifts_frame,
        shot_xg_frame,
        shots_frame,
    )
    from stint_fixtures import coverage_frame as coverage

    shot_xg = shot_xg_frame({1: 0.1, 2: 0.2}).with_columns(
        artifact_version=pl.Series(["xg-20261001-abc1234", "xg-20261002-def5678"])
    )
    with pytest.raises(ValueError, match="more than one model"):
        stints.build(
            coverage(),
            shifts_frame(full_period()),
            lineups_frame(),
            shots_frame([(1, 100, True, False), (2, 200, False, False)]),
            shot_xg,
            faceoffs_frame([]),
            calendar_frame(),
        )


def test_a_game_without_xg_has_null_xg_but_counts_its_goals() -> None:
    frame = build(full_period(), shots=[(1, 100, True, True)])
    row = frame.row(0, named=True)
    assert row["home_xg"] is None and row["xg_version"] is None
    assert row["home_goals"] == 1


def test_penalty_shots_stay_out_of_the_stints_totals() -> None:
    frame = build(full_period(), shots=[(1, 100, True, True)], xg={}, penalty_shots=(1,))
    assert frame["home_goals"].to_list() == [0, 0]
    # The goal still changes the score, so it cuts the stint.
    assert frame["score_state"].to_list() == [0, 1]


def test_an_incomplete_chart_gives_no_stints() -> None:
    assert build(full_period(), complete=False).is_empty()


def test_player_seconds_split_each_skaters_time_by_state() -> None:
    goalie = HOME_GOALIES[0]
    shifts = [*without(full_period(), 5), (5, 0, 400), (5, 520, PERIOD_END)]
    shifts = [*without(shifts, goalie), (goalie, 0, 1150), (6, 1150, PERIOD_END)]
    seconds = stints.player_seconds(build(shifts))
    one = dict(seconds.filter(pl.col("player_id") == 1).select("state", "seconds").rows())
    assert one == {"5v5": 400 + 630, "pk": 120, "other": 50}
    away = dict(seconds.filter(pl.col("player_id") == 11).select("state", "seconds").rows())
    assert away == {"5v5": 1030, "pp": 120, "other": 50}
    assert seconds.filter(pl.col("player_id") == goalie).is_empty()


def test_player_seconds_skip_stints_rapm_leaves_out() -> None:
    shifts = [*full_period(), (6, 400, 500), (7, 400, 500)]
    seconds = stints.player_seconds(build(shifts))
    assert seconds.filter(pl.col("player_id") == 6).is_empty()
    assert seconds.filter(pl.col("player_id") == 1)["seconds"].sum() == PERIOD_END - 100


def test_input_problems_name_games_without_coverage_and_seasons_without_xg() -> None:
    games = pl.DataFrame(
        {"game_id": [2019020001, 2019020002, 2010020001], "season": [SEASON, SEASON, 20102011]}
    )
    shot_xg = pl.DataFrame({"season": [20202021]})
    problems = stints.input_problems(games, coverage_frame(), shot_xg, [SEASON, 20102011])
    assert problems == [
        "20102011: 1 games without shift coverage, e.g. 2010020001",
        "20192020: 1 games without shift coverage, e.g. 2019020002",
        "20192020: no xG",
    ]
