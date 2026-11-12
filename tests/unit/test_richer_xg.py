"""scripts/richer_xg.py (#421): the previous play's coordinates, the score state, the added terms,
the fits beside ADR 0010's, the flurry discount, the decision and the report, on SYNTHETIC data."""

import math
from datetime import date
from typing import Any

import goalie_rest_fixtures as grf
import numpy as np
import polars as pl
import pytest
import richer_xg_fixtures as f
from shot_fixtures import games_from, synthetic_shots

from nhl_edge.backtest import yardstick as ys
from nhl_edge.features import xg

rx = f.load()
SEASONS = [20102011, 20112012, 20122013, 20132014, 20142015]


def test_the_previous_play_is_the_one_before_the_shot_turned_as_the_shot_is() -> None:
    plays = [
        f.play(1, "faceoff", "00:00", f.HOME, 0, 0, "N"),
        f.play(2, "shot-on-goal", "00:05", f.HOME, 70, 10, "O"),
        f.play(3, "stoppage", "00:07"),
        f.play(4, "missed-shot", "00:09", f.AWAY, -60, -5, "O"),
        f.play(5, "shot-on-goal", "00:11", f.AWAY, -80, 4, "O"),
    ]
    rows = {r["event_id"]: r for r in rx.previous_coordinates(f.body(plays), f.GAME_ID)}
    assert set(rows) == {2, 4, 5}
    # The home team attacks +x: the faceoff stays where it was.
    assert (rows[2]["prev_event_id"], rows[2]["prev_x"], rows[2]["prev_y"]) == (1, 0, 0)
    # A stoppage has no coordinates.
    assert rows[4]["prev_event_id"] == 3 and rows[4]["prev_x"] is None
    # The away team attacks -x: the play before is turned with the away shot.
    assert (rows[5]["prev_event_id"], rows[5]["prev_x"], rows[5]["prev_y"]) == (4, 60, 5)


def test_a_body_of_another_game_is_refused() -> None:
    with pytest.raises(ValueError, match="play-by-play"):
        rx.previous_coordinates(f.body([], game_id=1), f.GAME_ID)


def test_previous_frame_reads_each_games_own_response() -> None:
    shots = pl.DataFrame({"game_id": [f.GAME_ID, f.GAME_ID], "raw_key": ["a", "a"]})
    asked: list[str] = []

    def get(key: str) -> bytes:
        asked.append(key)
        return f.body([f.play(1, "shot-on-goal", "00:05", f.HOME, 70, 10, "O")])

    frame = rx.previous_frame(shots, get)
    assert asked == ["a"] and frame.height == 1 and frame["prev_event_id"].null_count() == 1


def _game(goals: list[tuple[int, bool]]) -> pl.DataFrame:
    """SYNTHETIC shots of one game: (sort_order, by the home team), each a goal."""
    return pl.DataFrame(
        {
            "game_id": [1] * len(goals),
            "event_id": list(range(1, len(goals) + 1)),
            "sort_order": [s for s, _ in goals],
            "is_home": [h for _, h in goals],
            "is_goal": [True] * len(goals),
        }
    )


def test_the_score_state_counts_earlier_goals_only() -> None:
    state = rx.score_state(_game([(10, True), (20, True), (30, False), (40, False), (50, False)]))
    rows = state.sort("event_id").iter_rows(named=True)
    found = [(r["leads"], r["trails"]) for r in rows]
    # Tied, home 1-0 (home leads), 2-0 (away trails), 2-1 (away trails), 2-2 (tied).
    assert found == [(0, 0), (1, 0), (0, 1), (0, 1), (0, 0)]


def _shots(rows: int = 6) -> pl.DataFrame:
    return (
        synthetic_shots([20132014], per_season=200)
        .filter(~pl.col("is_penalty_shot"), ~pl.col("is_empty_net"))
        .head(rows)
    )


def test_off_wing_from_the_hand_and_the_side() -> None:
    shots = _shots(4).with_columns(
        shooter_id=pl.Series([1, 1, 2, 3], dtype=pl.Int64),
        y=pl.Series([10, -10, -10, 10], dtype=pl.Int16),
    )
    frame = rx.richer_frame(shots, f.no_previous(shots), f.players({1: "R", 2: "L", 3: None}))
    # A right shot on +y and a left shot on -y are off-wing; an unknown hand never is.
    assert frame.sort("game_id", "event_id")["off_wing"].to_list() == [1.0, 0.0, 1.0, 0.0]


def test_the_previous_events_terms() -> None:
    shots = _shots(3).with_columns(
        x=pl.Series([60, 60, 60], dtype=pl.Int16),
        y=pl.Series([0, 0, 0], dtype=pl.Int16),
        prev_seconds=pl.Series([0, 4, 4], dtype=pl.Int32),
    )
    previous = f.no_previous(shots).with_columns(
        prev_x=pl.Series([30.0, 30.0, None]), prev_y=pl.Series([40.0, 40.0, None])
    )
    frame = rx.richer_frame(shots, previous, f.players({})).sort("game_id", "event_id")
    rows = list(frame.iter_rows(named=True))
    # 50 feet away: no time gives a speed of 50 feet a second (the time floored at 1).
    assert rows[0]["log_prev_distance"] == pytest.approx(math.log1p(50))
    assert rows[0]["log_prev_speed"] == pytest.approx(math.log1p(50))
    assert rows[1]["log_prev_seconds"] == pytest.approx(math.log1p(4))
    assert rows[1]["log_prev_speed"] == pytest.approx(math.log1p(12.5))
    assert rows[0]["no_prev_coordinates"] == 0.0
    # Without the previous play's coordinates, the three are 0 and the indicator 1.
    assert (rows[2]["log_prev_distance"], rows[2]["log_prev_seconds"]) == (0.0, 0.0)
    assert rows[2]["no_prev_coordinates"] == 1.0


def test_the_change_of_angle_counts_on_rebounds_only() -> None:
    shots = _shots(2).with_columns(
        x=pl.Series([79, 79], dtype=pl.Int16),
        y=pl.Series([10, 10], dtype=pl.Int16),
        prev_event_type=pl.Series(["shot-on-goal", "faceoff"]),
        prev_seconds=pl.Series([2, 2], dtype=pl.Int32),
        prev_by_shooting_team=pl.Series([True, True]),
    )
    previous = f.no_previous(shots).with_columns(
        prev_x=pl.Series([79.0, 79.0]), prev_y=pl.Series([-10.0, -10.0])
    )
    frame = rx.richer_frame(shots, previous, f.players({})).sort("game_id", "event_id")
    # From 45 degrees on one side to 45 on the other: 90 degrees, 1 once over 90.
    assert frame["angle_change"].to_list() == pytest.approx([1.0, 0.0])
    assert frame["rebound"].to_list() == [True, False]


def test_each_model_adds_its_terms_to_the_one_before() -> None:
    assert rx.terms("M0") == ()
    assert rx.terms("M2") == ("off_wing", "leads", "trails")
    assert len(rx.terms(rx.RICHER)) == sum(len(v) for v in rx.ADDED.values())


def _fitted(seed: int = 7) -> tuple[pl.DataFrame, pl.DataFrame, list[Any], pl.DataFrame]:
    shots = synthetic_shots(SEASONS, per_season=1500, seed=seed)
    rng = np.random.default_rng(seed)
    shots = shots.with_columns(
        shooter_id=pl.Series(rng.integers(1, 4, shots.height), dtype=pl.Int64)
    )
    previous = f.no_previous(shots).with_columns(
        prev_x=pl.Series(rng.normal(30, 20, shots.height)),
        prev_y=pl.Series(rng.normal(0, 20, shots.height)),
    )
    frame = rx.richer_frame(shots, previous, f.players({1: "L", 2: "R", 3: None}))
    games = games_from(shots)
    scored, fits = rx.predictions(frame, games.select("season", "start_utc"))
    return shots, games, fits, scored


SHOTS, GAMES, FITS, SCORED = _fitted()


def test_m0_is_adr_0010s_model() -> None:
    lake, _ = xg.score(SHOTS, GAMES, rx.SCORED, "xg-20261112-abcdef0")
    both = lake.join(SCORED.select("game_id", "event_id", "p_M0"), on=["game_id", "event_id"])
    assert both.height == lake.height == SCORED.height
    agree, largest = rx.gap(both["xg"], both["p_M0"])
    assert agree == both.height and largest < 1e-9


def test_every_level_on_the_explore_seasons_and_the_richer_one_on_all() -> None:
    explore = SCORED.filter(pl.col("season").is_in(ys.EXPLORE))
    rest = SCORED.filter(~pl.col("season").is_in(ys.EXPLORE))
    for level in rx.LEVELS:
        assert explore[f"p_{level}"].null_count() == 0
    assert rest["p_M0"].null_count() == 0 and rest[f"p_{rx.RICHER}"].null_count() == 0
    assert rest["p_M2"].null_count() == rest.height
    assert {(f.level, f.season) for f in FITS} >= {("M4", s) for s in rx.SCORED}


def test_step_one_pairs_each_level_and_calibrates_by_season() -> None:
    one = rx.step1(SCORED)
    assert set(one["levels"]) == set(rx.LEVELS)
    assert "vs_m0" not in one["levels"]["M0"] and "vs_before" in one["levels"]["M3"]
    assert set(one["calibration"]["M0"]) == {str(s) for s in ys.EXPLORE}
    assert 0.5 < one["levels"]["M0"]["auc"] < 1


def _chain_frames() -> tuple[pl.DataFrame, pl.DataFrame]:
    # A, B (rebound of A), C (rebound of B); D a rebound of a blocked shot (not scored); E a
    # rebound of the other team's shot; F, not a rebound, after C.
    frame = pl.DataFrame(
        {
            "game_id": [1] * 7,
            "event_id": [1, 2, 3, 5, 6, 7, 9],
            "team": ["MTL", "MTL", "MTL", "MTL", "TOR", "MTL", "MTL"],
            "rebound": [False, True, True, True, False, True, False],
            "prev_event_id": [None, 1, 2, 4, 5, 6, 3],
        },
        schema_overrides={"prev_event_id": pl.Int64},
    )
    today = pl.DataFrame(
        {
            "game_id": [1] * 7,
            "event_id": [1, 2, 3, 5, 6, 7, 9],
            "xg": [0.2, 0.5, 0.4, 0.3, 0.1, 0.3, 0.05],
        }
    )
    return frame, today


def test_a_flurry_discounts_each_shot_by_its_chains_earlier_shots() -> None:
    frame, today = _chain_frames()
    found = dict(rx.flurry(today, frame).select("event_id", "xg").iter_rows())
    assert found[1] == pytest.approx(0.2)
    assert found[2] == pytest.approx(0.5 * 0.8)
    assert found[3] == pytest.approx(0.4 * 0.8 * 0.5)
    # A rebound of an unscored attempt, of the other team's shot, or no rebound: no discount.
    assert found[5] == pytest.approx(0.3) and found[7] == pytest.approx(0.3)
    assert found[9] == pytest.approx(0.05)


def test_as_shot_xg_needs_every_shot() -> None:
    today = pl.DataFrame({"game_id": [1, 1], "event_id": [1, 2], "xg": [0.1, 0.2]})
    scored = pl.DataFrame({"game_id": [1], "event_id": [1], "p_M4": [0.3]})
    with pytest.raises(ValueError, match="1 of today's xG shots without M4"):
        rx.as_shot_xg(scored, today, "M4")


def _per_game(shifts: dict[str, float], seed: int = 1) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    days = [date(2013, 10, 1 + d % 28) for d in range(400)]
    seasons = [20132014 if i < 200 else 20142015 for i in range(400)]
    base = rng.normal(0, 1, 400)
    frames = []
    for name in rx.CANDIDATES:
        frames.append(
            pl.DataFrame(
                {
                    "candidate": name,
                    "game_id": range(400),
                    "season": seasons,
                    "game_date": days,
                    "rested": [i % 3 != 0 for i in range(400)],
                    "move": base + shifts.get(name, 0.0) + rng.normal(0, 0.01, 400),
                    "result": rng.normal(0, 0.01, 400),
                }
            )
        )
    return pl.concat(frames)


def test_decide_holds_only_a_negative_passing_test() -> None:
    found = rx.decide(_per_game({"richer 5v5": -0.05, "richer goalie": 0.05}))
    estimates = found["estimates"]
    assert estimates["richer 5v5"]["holds"]
    assert not estimates["richer goalie"]["holds"] and not estimates["flurry 5v5"]["holds"]
    assert set(estimates["flurry 5v5"]["per_season"]) == {"20132014", "20142015"}


def test_the_report_has_every_step() -> None:
    read = grf.league_read()
    rng = np.random.default_rng(3)
    frame = read.frame.with_columns(
        pl.Series(column, rng.normal(0, 0.1, read.frame.height)) for column in rx.PARTS.values()
    )
    per_game, figures = ys.run(frame, "explore", rx.CANDIDATES)
    found = {
        "step1": rx.step1(SCORED),
        "counts": {
            "no_hand": 1,
            "no_prev": 2,
            "rebounds": 3,
            "chained": 4,
            "mean_discount": 0.9,
            "xg_today": 10.0,
            "xg_flurry": 9.5,
        },
        "checks": {
            "m0_agree": 5,
            "m0_shots": 5,
            "m0_gap": 0.0,
            "five_agree": 6,
            "five_games": 6,
            "five_gap": 0.0,
            "goalie_agree": 6,
            "games": 6,
            "goalie_gap": 0.0,
        },
        "tuning_cutoff": "2018-04-09 10:00:00+00:00",
        "figures": figures,
        "calibration": rx.calibration(frame),
        "decision": rx.decide(per_game),
    }
    text = rx.markdown(found, "richer-xg-20261112-abcdef0")
    for heading in ("## Step 1: the fit", "## Step 2: flurries", "## Step 3", "## The decision"):
        assert heading in text
    assert "on 5 of 5 shots" in text and "| M4 | angle_change |" in text
