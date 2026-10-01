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
    xg = xg.filter(pl.col("game_id") == game["game_id"]).with_columns(goalie_in=ts.own_goalie_in())

    def total(team: str, skaters: tuple[int, int], goalie_in: bool = True) -> float:
        rows_ = xg.filter(
            pl.col("team") == team,
            pl.col("skaters_for") == skaters[0],
            pl.col("skaters_against") == skaters[1],
            pl.col("goalie_in") == goalie_in,
        )
        return float(rows_["xg"].sum())

    assert home["xgf_5v5"] == pytest.approx(total(game["home"], (5, 5)))
    assert home["xga_5v5"] == pytest.approx(total(game["away"], (5, 5)))
    # A 5-on-5 shot with the shooting team's own goalie pulled counts for neither side (Codex on
    # #86): the minutes count only with both nets manned.
    assert total(game["home"], (5, 5), goalie_in=False) > 0
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
        # A game starting before 10:00 ET is rated an hour before its start (Codex on #86).
        (
            date(2026, 10, 15),
            datetime(2026, 10, 15, 13, tzinfo=UTC),
            datetime(2026, 10, 15, 12, tzinfo=UTC),
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
    assert (frame["as_of_utc"] < LEAGUE["games"].sort("game_id")["start_utc"]).all()
    # These seasons were tuned on: their ratings count as known only from the tuning cutoff.
    assert set(frame["observed_utc"]) == {ts.TUNED_CUTOFF}


def test_input_problems_name_games_without_xg_or_strength_time() -> None:
    games, shot_xg, time_on = LEAGUE["games"], LEAGUE["shot_xg"], LEAGUE["strength_time"]
    expected = dict(games.group_by("season").len().iter_rows())
    assert ts.input_problems(games, shot_xg, time_on, 20122013, expected) == []
    first = games["game_id"][0]
    problems = ts.input_problems(
        games, shot_xg.filter(pl.col("game_id") != first), time_on, 20122013, expected
    )
    assert problems == [f"20112012: 1 games without xG, e.g. {first}"]


def test_a_season_short_of_its_games_is_a_problem() -> None:
    # Codex on #86: a game missing from games entirely is caught by the season's count.
    games = LEAGUE["games"]
    expected = dict(games.group_by("season").len().iter_rows())
    short = games.filter(pl.col("game_id") != games["game_id"][0])
    problems = ts.input_problems(
        short, LEAGUE["shot_xg"], LEAGUE["strength_time"], 20122013, expected
    )
    assert problems == [f"20112012: {expected[20112012] - 1:,} of {expected[20112012]:,} games"]


def test_a_team_without_special_teams_history_keeps_its_5v5_part() -> None:
    # Codex on #86: with no pull toward the league, a team with games but no power-play or
    # penalty-kill minutes has no special-teams rate; its 5v5 part still counts.
    no_special = HISTORY.with_columns(
        pl.when(pl.col("team") == "BOS").then(0.0).otherwise(pl.col(c)).alias(c)
        for c in ("xgf_pp", "xga_pk", "min_pp", "min_pk")
    )
    later = LEAGUE["games"].filter(
        pl.col("season") == 20122013, (pl.col("home") == "BOS") | (pl.col("away") == "BOS")
    )
    frame = ts.strength(later, no_special, ts.Settings(half_life=20, prior_games=0))
    assert (frame["delta_5v5"].abs() > 0).all()
    assert (frame["delta_s"] == frame["delta_5v5"] + frame["delta_special_teams"]).all()


def test_a_renamed_team_keeps_its_history() -> None:
    # Codex on #86: TOR plays the second season as XYZ. Linked as one line, its ratings read its
    # first season; unlinked, it starts from nothing.
    renamed = {
        name: frame.with_columns(
            pl.when((pl.col(col) == "TOR") & (pl.col("season") == 20122013))
            .then(pl.lit("XYZ"))
            .otherwise(pl.col(col))
            .alias(col)
            for col in cols
        )
        for name, frame, cols in (
            ("games", LEAGUE["games"], ("home", "away")),
            ("strength_time", LEAGUE["strength_time"], ("team",)),
        )
    }
    shots = (
        LEAGUE["shots"]
        .join(LEAGUE["games"].select("game_id", "season"), on="game_id")
        .with_columns(
            team=pl.when((pl.col("team") == "TOR") & (pl.col("season") == 20122013))
            .then(pl.lit("XYZ"))
            .otherwise(pl.col("team"))
        )
    )
    history = ts.team_games(shots, LEAGUE["shot_xg"], renamed["strength_time"])
    games = renamed["games"].filter(pl.col("season") == 20122013, pl.col("home") == "XYZ").head(3)
    lines = {team: team for team in ("BOS", "BUF", "DET", "FLA", "MTL", "TOR")}
    linked = ts.strength(games, history, SETTINGS, {**lines, "XYZ": "TOR"})
    unlinked = ts.strength(games, history, SETTINGS, lines)
    assert (linked["home_history"] > unlinked["home_history"] + 30).all()
    assert (linked["delta_s"] < 0).all()  # TOR, the weakest team, is rated as such at once


def test_the_grid_and_its_steadiness_order() -> None:
    assert len(ts.GRID) == 16 and ts.TUNED in ts.GRID
    steadiest = max(ts.GRID, key=ts.steadiness)
    assert steadiest == ts.Settings(80, 40)
    assert ts.steadiness(ts.Settings(80, 0)) > ts.steadiness(ts.Settings(40, 40))


def test_a_team_without_xg_rows_in_a_game_keeps_the_game_with_none() -> None:
    # Codex on #86: if one team's attempts in a game all lack xG (no coordinates, say), the game
    # stays in both teams' histories, with no xG for that team.
    game = LEAGUE["games"].row(0, named=True)
    shots_of_home = LEAGUE["shots"].filter(
        pl.col("game_id") == game["game_id"], pl.col("team") == game["home"]
    )
    missing = LEAGUE["shot_xg"].join(
        shots_of_home.select("game_id", "event_id"), on=["game_id", "event_id"], how="anti"
    )
    history = ts.team_games(LEAGUE["shots"], missing, LEAGUE["strength_time"])
    rows = history.filter(pl.col("game_id") == game["game_id"])
    assert rows.height == 2
    home = rows.filter(pl.col("team") == game["home"]).row(0, named=True)
    away = rows.filter(pl.col("team") == game["away"]).row(0, named=True)
    assert (home["xgf_5v5"], home["xgf_pp"], away["xga_5v5"], away["xga_pk"]) == (0, 0, 0, 0)
    assert home["min_5v5"] == 48.0 and away["xgf_5v5"] > 0
