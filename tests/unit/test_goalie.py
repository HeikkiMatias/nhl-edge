from datetime import UTC, date, datetime

import polars as pl
import pytest
from goalie_fixtures import GOALIES, league, public_after, start_of, with_shots

from nhl_edge.features import goalie as ge
from nhl_edge.features import team_strength as ts
from nhl_edge.lake.schemas import GoalieEffects
from nhl_edge.lineup import goalie_start as gs

LEAGUE = with_shots(league())
GAMES = LEAGUE["games"]
VERSION = "goalie-effect-20261001-abc1234"
GOALIES_HISTORY = ge.goalie_games(LEAGUE["shots"], LEAGUE["shot_xg"])
TEAM_SHOTS = ge.team_shot_games(GAMES, LEAGUE["shots"], LEAGUE["shot_xg"])
STARTS, _, _ = gs.score(
    LEAGUE["lineups"], GAMES, [20112012, 20122013], "goalie-start-20261001-abc1234", {}
)
SETTINGS = ge.Settings(half_life=20, prior_shots=200)


def rated(settings: ge.Settings = SETTINGS) -> pl.DataFrame:
    games = GAMES.filter(pl.col("season") >= 20112012)
    return ge.effects(STARTS, games, GOALIES_HISTORY, TEAM_SHOTS, settings, {})


def game(game_id: int, day: date, home: str, away: str) -> dict[str, object]:
    return {
        "game_id": game_id,
        "season": 20112012,
        "game_date": day,
        "start_utc": start_of(day),
        "home": home,
        "away": away,
        "observed_utc": public_after(day),
    }


def faced(rows: list[tuple[int, date, int, float, bool]]) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Shots and their xG, from (game_id, game_date, goalie_id, xg, is_goal) per shot."""
    shots = pl.DataFrame(
        [
            {"game_id": g, "event_id": i, "team": "BUF", "goalie_id": goalie, "is_goal": goal}
            for i, (g, _, goalie, _, goal) in enumerate(rows)
        ]
    )
    xg = pl.DataFrame(
        [
            {
                "game_id": g,
                "season": 20112012,
                "game_date": day,
                "event_id": i,
                "xg": chance,
                "observed_utc": public_after(day),
            }
            for i, (g, day, _, chance, _) in enumerate(rows)
        ]
    ).with_columns(pl.col("season").cast(pl.Int32))
    return shots, xg


def test_goalie_games_sum_his_shots_and_goals_saved_above_expected() -> None:
    shots, xg = faced(
        [
            (1, date(2011, 10, 5), 30, 0.25, False),
            (1, date(2011, 10, 5), 30, 0.5, True),
            (1, date(2011, 10, 5), 31, 0.25, True),
            (2, date(2011, 10, 7), 30, 0.25, False),
        ]
    )
    frame = ge.goalie_games(shots, xg)
    rows = {(r["goalie_id"], r["game_id"]): r for r in frame.iter_rows(named=True)}
    assert (rows[(30, 1)]["shots"], rows[(30, 1)]["gsax"]) == (2.0, pytest.approx(-0.25))
    assert (rows[(31, 1)]["shots"], rows[(31, 1)]["gsax"]) == (1.0, pytest.approx(-0.75))
    assert (rows[(30, 2)]["shots"], rows[(30, 2)]["gsax"]) == (1.0, pytest.approx(0.25))


def test_an_effect_is_decayed_by_his_games_and_shrunk_toward_zero() -> None:
    # Goalie 30: +0.5 on 10 shots, then -0.5 on 20, for two different teams; the game to rate is
    # a third team's. With a half-life of one game the older weighs 0.5.
    shots, xg = faced(
        [(1, date(2011, 10, 5), 30, 0.05, False)] * 10
        + [(2, date(2011, 10, 7), 30, 0.025, k == 0) for k in range(20)]
    )
    history = ge.goalie_games(shots, xg)
    gsax = history.sort("game_id")["gsax"].to_list()
    assert gsax == pytest.approx([0.5, -0.5])
    games = pl.DataFrame([game(3, date(2011, 10, 9), "DET", "TOR")]).with_columns(
        pl.col("season").cast(pl.Int32)
    )
    candidate = pl.DataFrame({"game_id": [3], "team": ["DET"], "goalie_id": [30]})
    team_shots = ge.team_shot_games(games, shots, xg)
    out = ge.effects(candidate, games, history, team_shots, ge.Settings(1, 15), {})
    expected = (0.5 * gsax[0] + gsax[1]) / (0.5 * 10 + 20 + 15)
    assert out["effect"][0] == pytest.approx(expected)
    assert out["goalie_games"][0] == 2


def test_a_goalie_with_no_history_has_no_effect() -> None:
    first_night = rated().filter(pl.col("game_date") == pl.col("game_date").min())
    assert (first_night["effect"] == 0).all() and (first_night["goalie_games"] == 0).all()


def test_good_goalies_rate_above_bad_ones() -> None:
    late = rated().filter(pl.col("season") == 20122013, pl.col("goalie_games") > 20)
    starters = {first for first, _ in GOALIES.values()}
    by_goalie = late.group_by("goalie_id").agg(pl.col("effect").mean())
    for goalie, effect in by_goalie.iter_rows():
        assert (effect > 0) is (goalie in starters)


def test_expected_shots_shrink_toward_the_league() -> None:
    frame = rated()
    assert frame["expected_shots"].min() == 0.0  # the first night, before any shot is public
    later = frame.filter(pl.col("season") == 20122013)
    # Every team takes 30 unblocked shots a game, so the league rate and every team's is 30.
    assert later["expected_shots"].to_numpy() == pytest.approx(30.0)
    assert (frame["goals_saved"] - frame["effect"] * frame["expected_shots"]).abs().max() < 1e-12  # type: ignore[operator]


def test_expected_delta_weighs_each_side_by_its_start_probabilities() -> None:
    games = pl.DataFrame({"game_id": [1, 2], "home": ["BOS", "DET"], "away": ["BUF", "TOR"]})
    effects = pl.DataFrame(
        {
            "game_id": [1, 1, 1, 2],
            "team": ["BOS", "BOS", "BUF", "DET"],
            "goalie_id": [10, 11, 20, 30],
            "goals_saved": [1.0, -1.0, 0.5, 0.2],
        }
    )
    starts = effects.select("game_id", "team", "goalie_id").with_columns(
        p_start=pl.Series([0.75, 0.25, 1.0, 1.0])
    )
    x = dict(ge.expected_delta(effects, starts, games).iter_rows())
    assert x[1] == pytest.approx(0.75 - 0.25 - 0.5)
    # TOR has no candidates and adds nothing.
    assert x[2] == pytest.approx(0.2)


def test_rows_carry_the_settings_and_the_tuning_cutoff() -> None:
    frame = ge.rows(rated(), SETTINGS, VERSION)
    GoalieEffects.validate(frame)
    assert set(frame["train_cutoff"]) == {ge.TUNED_CUTOFF}
    assert (frame["observed_utc"] == ge.TUNED_CUTOFF).all()  # the fixture predates the cutoff
    later = ge.rows(rated(), SETTINGS, VERSION, train_cutoff=datetime(2010, 1, 1, tzinfo=UTC))
    assert (later["observed_utc"] == later["as_of_utc"]).all()
    assert set(later["half_life"]) == {20.0} and set(later["prior_shots"]) == {200.0}


def test_the_grid_and_its_steadiness_order() -> None:
    assert len(ge.GRID) == 16 and ge.TUNED in ge.GRID
    steadiest = max(ge.GRID, key=ge.steadiness)
    assert (steadiest.half_life, steadiest.prior_shots) == (160, 4000)
    assert ge.TUNING_SEASONS == ts.TUNING_SEASONS


def test_input_problems() -> None:
    games = GAMES.filter(pl.col("season") >= 20112012)
    counts = dict(games.group_by("season").len().iter_rows())
    assert ge.input_problems(games, LEAGUE["shot_xg"], STARTS, 20122013, counts) == []
    first = games["game_id"][0]
    no_xg = LEAGUE["shot_xg"].filter(pl.col("game_id") != first)
    assert ge.input_problems(games, no_xg, STARTS, 20122013, counts) == [
        f"20112012: 1 games without xG, e.g. {first}"
    ]
    no_starts = STARTS.filter(pl.col("game_id") != first)
    assert ge.input_problems(games, LEAGUE["shot_xg"], no_starts, 20122013, counts) == [
        f"20112012: 1 games without goalie starts, e.g. {first}"
    ]
