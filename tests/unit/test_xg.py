from datetime import UTC, datetime

import numpy as np
import polars as pl
import pytest
from shot_fixtures import games_of, synthetic_shots

from nhl_edge.audit import xg as report
from nhl_edge.features import xg
from nhl_edge.lake.schemas import ShotXg

SEASONS = [20102011, 20112012, 20122013]
SHOTS = synthetic_shots(SEASONS)
GAMES = games_of(SEASONS)
VERSION = "xg-20260930-abc1234"


def one_shot(**values: object) -> pl.DataFrame:
    row = SHOTS.filter(~pl.col("is_penalty_shot"), ~pl.col("is_empty_net")).head(1)
    return row.with_columns(
        **{name: pl.lit(value, dtype=row.schema[name]) for name, value in values.items()}
    )


# Inputs


@pytest.mark.parametrize(
    ("x", "y", "distance", "angle"),
    [(59, 0, 30.0, 0.0), (89, 20, 20.0, 90.0), (79, 10, 10 * 2**0.5, 45.0), (99, 0, 10.0, 180.0)],
)
def test_distance_and_angle_to_the_attacked_net(
    x: int, y: int, distance: float, angle: float
) -> None:
    frame = xg.model_frame(one_shot(x=x, y=y))
    assert frame["distance"].item() == pytest.approx(distance)
    assert frame["angle"].item() == pytest.approx(angle)


def test_penalty_shots_empty_nets_and_shots_without_coordinates_are_left_out() -> None:
    frame = xg.model_frame(SHOTS)
    assert frame.height == SHOTS.filter(~pl.col("is_penalty_shot"), ~pl.col("is_empty_net")).height
    assert xg.model_frame(one_shot(x=None)).is_empty()


def flags(**values: object) -> tuple[bool, bool]:
    row = xg.model_frame(one_shot(**values)).row(0, named=True)
    return row["rebound"], row["rush"]


def test_rebound_is_the_shooting_teams_attempt_three_seconds_or_less_before() -> None:
    kept = {"prev_zone": "O", "prev_by_shooting_team": True}
    assert flags(prev_event_type="blocked-shot", prev_seconds=3, **kept) == (True, False)
    assert flags(prev_event_type="shot-on-goal", prev_seconds=4, **kept) == (False, False)
    assert flags(prev_event_type="hit", prev_seconds=1, **kept) == (False, False)
    other_team = {"prev_zone": "O", "prev_by_shooting_team": False}
    assert flags(prev_event_type="missed-shot", prev_seconds=1, **other_team) == (False, False)


def test_rush_is_a_play_outside_the_offensive_zone_four_seconds_or_less_before() -> None:
    kept = {"prev_event_type": "takeaway", "prev_by_shooting_team": True}
    assert flags(prev_zone="D", prev_seconds=4, **kept) == (False, True)
    assert flags(prev_zone="N", prev_seconds=2, **kept) == (False, True)
    assert flags(prev_zone="N", prev_seconds=5, **kept) == (False, False)
    assert flags(prev_zone="O", prev_seconds=1, **kept) == (False, False)
    assert flags(prev_event_type=None, prev_zone=None, prev_seconds=None) == (False, False)


STATES = [
    (5, 5, "5v5"),
    (4, 4, "4v4"),
    (3, 3, "3v3"),
    (5, 4, "advantage"),
    (6, 5, "advantage"),
    (4, 5, "shorthanded"),
    (None, None, "unknown"),
]


@pytest.mark.parametrize(("skaters_for", "skaters_against", "state"), STATES)
def test_strength_state_from_the_shooting_teams_side(
    skaters_for: int | None, skaters_against: int | None, state: str
) -> None:
    frame = xg.model_frame(one_shot(skaters_for=skaters_for, skaters_against=skaters_against))
    assert frame["state"].item() == state


def test_rare_and_unknown_shot_types_are_other() -> None:
    for shot_type in ("bat", None):
        assert xg.model_frame(one_shot(shot_type=shot_type))["shot_kind"].item() == "other"


# Fit and score


def test_the_fit_recovers_the_effects_behind_the_goals() -> None:
    start = datetime(2012, 10, 5, 23, tzinfo=UTC)
    model = xg.fit(SHOTS, 20122013, start, VERSION)
    assert model.seasons == (20102011, 20112012)
    base = one_shot(season=20112012, x=69, y=0, shot_type="wrist")
    near, far = base.with_columns(x=pl.lit(84, pl.Int16)), base.with_columns(x=pl.lit(39, pl.Int16))
    rebound = base.with_columns(prev_event_type=pl.lit("shot-on-goal"), prev_seconds=pl.lit(1))
    rush = base.with_columns(prev_zone=pl.lit("N"), prev_seconds=pl.lit(2))
    plain = base.with_columns(prev_event_type=pl.lit("faceoff"), prev_seconds=pl.lit(20))
    p = {
        name: model.predict(xg.model_frame(frame)).item()
        for name, frame in {"near": near, "far": far, "rebound": rebound, "rush": rush}.items()
    } | {"plain": model.predict(xg.model_frame(plain)).item()}
    assert p["near"] > p["plain"] > p["far"]
    assert p["rebound"] > p["rush"] > p["plain"]
    train = xg.model_frame(SHOTS.filter(pl.col("season") < 20122013))
    assert model.predict(train).mean() == pytest.approx(train["is_goal"].mean(), abs=1e-3)


def test_a_season_the_model_has_not_seen_takes_the_last_seasons_level() -> None:
    model = xg.fit(SHOTS, 20122013, datetime(2012, 10, 5, 23, tzinfo=UTC), VERSION)
    frame = xg.model_frame(one_shot())
    last = model.predict(frame.with_columns(season=pl.lit(20112012, pl.Int32)))
    later = model.predict(frame.with_columns(season=pl.lit(20122013, pl.Int32)))
    assert later == pytest.approx(last)


def test_score_gives_every_modelled_shot_of_each_season_an_xg() -> None:
    scored, models = xg.score(SHOTS, GAMES, [20112012, 20122013], VERSION)
    ShotXg.validate(scored)
    expected = xg.model_frame(SHOTS.filter(pl.col("season") > 20102011)).height
    assert scored.height == expected
    assert [m.season for m in models] == [20112012, 20122013]
    assert set(scored["artifact_version"]) == {VERSION}
    for model in models:
        rows = scored.filter(pl.col("season") == model.season)
        assert (rows["train_cutoff"] == model.train_cutoff).all()


def test_the_first_season_has_nothing_to_train_on() -> None:
    with pytest.raises(ValueError, match="no earlier season"):
        xg.score(SHOTS, GAMES, [20102011], VERSION)


# Calibration report


def test_the_report_shows_figures_for_open_seasons_only() -> None:
    scored, models = xg.score(SHOTS, GAMES, [20112012, 20122013], VERSION)
    shots = report.scored_shots(scored, SHOTS)
    rows = report.season_report(shots, open_seasons=[20112012])
    assert rows[0]["season"] == 20112012 and "auc" in rows[0]
    assert rows[1] == {
        "season": 20122013,
        "shots": shots.filter(pl.col("season") == 20122013).height,
    }
    assert rows[0]["low"] < rows[0]["difference"] < rows[0]["high"]
    text = report.markdown_report(shots, models, [20112012], VERSION)
    assert "| 20122013 | " in text and "held out" in text
    assert "## Rebound, open seasons 20112012" in text
    assert "| 0-10 ft |" in text or "| 10-20 ft |" in text
    assert np.isclose(sum(r["shots"] for r in report.group_report(shots, "rush")), shots.height)
