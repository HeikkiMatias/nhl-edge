from datetime import UTC, date, datetime

import polars as pl
import pytest
from team_fixtures import QUALITY, league

from nhl_edge.features import team_strength as ts
from nhl_edge.lake.schemas import TeamStrength

LEAGUE = league()
HISTORY = ts.team_games(LEAGUE["shots"], LEAGUE["shot_xg"], LEAGUE["strength_time"])
SETTINGS = ts.Settings(half_life=20, prior_games=10)


def rated(games: pl.DataFrame = LEAGUE["games"], settings: ts.Settings = SETTINGS) -> pl.DataFrame:
    return ts.strength(games, HISTORY, settings)


def test_team_games_split_xg_and_minutes_by_state() -> None:
    game = LEAGUE["games"].row(0, named=True)
    rows = HISTORY.filter(pl.col("game_id") == game["game_id"])
    home = rows.filter(pl.col("team") == game["home"]).row(0, named=True)
    away = rows.filter(pl.col("team") == game["away"]).row(0, named=True)
    xg = LEAGUE["shot_xg"].join(LEAGUE["shots"], on=["game_id", "event_id"])
    xg = xg.filter(pl.col("game_id") == game["game_id"])

    def total(team: str, skaters: tuple[int, int]) -> float:
        rows_ = xg.filter(
            pl.col("team") == team,
            pl.col("skaters_for") == skaters[0],
            pl.col("skaters_against") == skaters[1],
        )
        return float(rows_["xg"].sum())

    assert home["xgf_5v5"] == pytest.approx(total(game["home"], (5, 5)))
    assert home["xga_5v5"] == pytest.approx(total(game["away"], (5, 5)))
    # Power-play xG counts for the team on the advantage and against the other's penalty kill;
    # 6v5 shots, with the shooting team's goalie pulled, count for neither.
    assert home["xgf_pp"] == pytest.approx(total(game["home"], (5, 4)))
    assert away["xga_pk"] == pytest.approx(total(game["home"], (5, 4)))
    assert (home["min_5v5"], home["min_pp"], home["min_pk"]) == (48.0, 4.0, 4.0)


def test_a_stronger_home_team_has_a_positive_delta() -> None:
    frame = rated().filter(pl.col("season") == 20122013)
    best, worst = max(QUALITY, key=QUALITY.__getitem__), min(QUALITY, key=QUALITY.__getitem__)
    assert (frame.filter(pl.col("home") == best)["delta_s"] > 0).all()
    assert (frame.filter(pl.col("home") == worst)["delta_s"] < 0).all()
    assert (
        frame.select(
            (pl.col("delta_s") - pl.col("delta_5v5") - pl.col("delta_special_teams")).abs().max()
        ).item()
        < 1e-12
    )


def test_swapping_home_and_away_flips_the_sign() -> None:
    games = LEAGUE["games"].filter(pl.col("season") == 20122013).head(10)
    swapped = games.with_columns(home=pl.col("away"), away=pl.col("home"))
    assert rated(swapped)["delta_s"].to_list() == pytest.approx(
        [-v for v in rated(games)["delta_s"].to_list()]
    )


def test_teams_with_no_history_are_even() -> None:
    first_night = LEAGUE["games"].filter(pl.col("game_date") == LEAGUE["games"]["game_date"].min())
    frame = rated(first_night)
    assert (frame["delta_s"] == 0).all()
    assert (frame["home_history"] == 0).all()


def test_decay_by_games_played() -> None:
    # Ten games of one xG each: with a half-life of 2, the running sum is sum(0.5 ** (k / 2)).
    history = pl.DataFrame(
        {
            "team": ["BOS"] * 10,
            "season": [20112012] * 10,
            "game_date": [date(2011, 10, d + 1) for d in range(10)],
            "game_id": list(range(10)),
            **{name: [1.0] * 10 for name in ts.SUMS},
            "observed_utc": [datetime(2011, 10, d + 2, 10, tzinfo=UTC) for d in range(10)],
        }
    )
    targets = pl.DataFrame(
        {
            "team": ["BOS"],
            "season": [20112012],
            "as_of_utc": [datetime(2011, 10, 20, 14, tzinfo=UTC)],
        }
    )
    state = ts.team_states(history, targets, ts.Settings(half_life=2, prior_games=0)).row(
        0, named=True
    )
    assert state["games"] == pytest.approx(sum(0.5 ** (k / 2) for k in range(10)))
    assert state["history_games"] == 10


@pytest.mark.parametrize(
    ("game_date", "start", "expected"),
    [
        # 10:00 EDT is 14:00 UTC, and 10:00 EST is 15:00 UTC.
        (
            date(2026, 10, 15),
            datetime(2026, 10, 15, 23, tzinfo=UTC),
            datetime(2026, 10, 15, 14, tzinfo=UTC),
        ),
        (
            date(2026, 12, 15),
            datetime(2026, 12, 16, 0, tzinfo=UTC),
            datetime(2026, 12, 15, 15, tzinfo=UTC),
        ),
        # A game starting before 10:00 ET is rated as of its start.
        (
            date(2026, 10, 15),
            datetime(2026, 10, 15, 13, tzinfo=UTC),
            datetime(2026, 10, 15, 13, tzinfo=UTC),
        ),
    ],
)
def test_ratings_are_as_of_ten_eastern_or_the_start(
    game_date: date, start: datetime, expected: datetime
) -> None:
    frame = pl.DataFrame({"game_date": [game_date], "start_utc": [start]}).with_columns(
        pl.col("start_utc").dt.cast_time_unit("us")
    )
    assert frame.select(ts.as_of(pl.col("game_date"), pl.col("start_utc"))).item() == expected


def test_rows_validate_and_carry_the_settings() -> None:
    frame = ts.rows(LEAGUE["games"], HISTORY, ts.TUNED, "team-strength-20261001-abc1234")
    TeamStrength.validate(frame)
    assert set(frame["half_life"]) == {80.0} and set(frame["prior_games"]) == {40.0}
    assert (frame["observed_utc"] <= LEAGUE["games"].sort("game_id")["start_utc"]).all()


def test_input_problems_name_games_without_xg_or_strength_time() -> None:
    games, shot_xg, time_on = LEAGUE["games"], LEAGUE["shot_xg"], LEAGUE["strength_time"]
    assert ts.input_problems(games, shot_xg, time_on, 20122013) == []
    first = games["game_id"][0]
    problems = ts.input_problems(
        games, shot_xg.filter(pl.col("game_id") != first), time_on, 20122013
    )
    assert problems == [f"20112012: 1 games without xG, e.g. {first}"]


def test_the_grid_and_its_steadiness_order() -> None:
    assert len(ts.GRID) == 16 and ts.TUNED in ts.GRID
    steadiest = max(ts.GRID, key=ts.steadiness)
    assert steadiest == ts.Settings(80, 40)
    assert ts.steadiness(ts.Settings(80, 0)) > ts.steadiness(ts.Settings(40, 40))
