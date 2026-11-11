"""scripts/goals_attempts.py (#420): the 5v5 goals and attempts each team-game counts, the rates
built as ΔS's 5v5 part is, the pull's choice, the decision and the report, on SYNTHETIC data."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast

import goalie_rest_fixtures as grf
import goals_attempts_fixtures as f
import numpy as np
import polars as pl
import pytest

from nhl_edge.backtest import yardstick as ys
from nhl_edge.features import team_strength as ts

ga = f.load()
LEAGUE = f.league()
HISTORY = f.history(LEAGUE)
GAMES = LEAGUE["games"]


def shot(
    for_: int,
    against: int,
    event: str = "shot-on-goal",
    goalie_in: bool = True,
    empty_net: bool = False,
    penalty: bool = False,
) -> dict[str, Any]:
    own = "1" if goalie_in else "0"
    return {
        "game_id": 1,
        "team": "BOS",
        "is_home": True,
        "skaters_for": for_,
        "skaters_against": against,
        "situation_code": f"1{against}{for_}{own}",
        "event_type": event,
        "is_empty_net": empty_net,
        "is_penalty_shot": penalty,
    }


def test_counts_keep_5v5_with_both_goalies_in_and_blocks_by_reason() -> None:
    shots = pl.DataFrame(
        [
            shot(5, 5, "goal"),
            shot(5, 5, "missed-shot"),
            shot(5, 5, "goal", goalie_in=False),
            shot(5, 4, "goal"),
            shot(5, 5, "goal", empty_net=True),
            shot(5, 5, "goal", penalty=True),
        ]
    )
    blocked = pl.DataFrame(
        [
            {**shot(5, 5), "reason": reason}
            for reason in (None, "blocked", "teammate-blocked", "other-block")
        ]
        + [{**shot(5, 4), "reason": None}],
        schema_overrides={"reason": pl.String},
    ).drop("event_type", "is_penalty_shot")
    row = ga.counts(shots, blocked).row(0, named=True)
    # One goal and one miss count; two blocks count, a teammate's and "other-block" don't.
    assert (row["goals"], row["attempts"]) == (1.0, 4.0)


def test_team_games_mirror_each_side_and_keep_team_strengths_rows() -> None:
    base = ts.team_games(LEAGUE["shots"], LEAGUE["shot_xg"], LEAGUE["strength_time"])
    assert HISTORY.height == base.height
    joined = HISTORY.join(base, on=["game_id", "team"], suffix="_ts")
    for column in ("xgf_5v5", "xga_5v5", "min_5v5", "observed_utc"):
        assert joined[column].equals(joined[f"{column}_ts"])
    mirror = HISTORY.join(
        HISTORY.select("game_id", team="opponent", gf_other="gf", cf_other="cf"),
        on=["game_id", "team"],
    )
    assert mirror["ga"].equals(mirror["gf_other"]) and mirror["ca"].equals(mirror["cf_other"])
    # Each side's attempts: its 5v5 shots with its goalie in (25) and two 5v5 blocks.
    assert (HISTORY["cf"] == 27).all()
    assert (HISTORY["gf"] <= 25).all() and HISTORY["gf"].sum() > 0


@pytest.mark.parametrize("pull", [0.0, 10.0])
def test_the_xg_column_is_team_strengths_5v5_part(pull: float) -> None:
    settings = ts.Settings(half_life=20, prior_games=pull)
    expected = ts.strength(
        GAMES, ts.team_games(LEAGUE["shots"], LEAGUE["shot_xg"], LEAGUE["strength_time"]), settings
    )
    mine = ga.columns_for(GAMES, HISTORY, {m: pull for m in ga.MEASURES}, half_life=20)
    both = mine.join(expected.select("game_id", "delta_5v5", "as_of_utc"), on="game_id")
    assert both.height == GAMES.height
    assert both["as_of_utc"].equals(both["as_of_utc_right"])
    assert (both["five_team_xg_diff"] - both["delta_5v5"]).abs().max() < 1e-12  # type: ignore[operator]


def test_swapping_home_and_away_flips_every_column() -> None:
    games = GAMES.filter(pl.col("season") == 20122013).head(10)
    swapped = games.with_columns(home=pl.col("away"), away=pl.col("home"))
    pulls = {m: 20.0 for m in ga.MEASURES}
    a = ga.columns_for(games, HISTORY, pulls)
    b = ga.columns_for(swapped, HISTORY, pulls)
    for column in ga.COLUMNS.values():
        assert b[column].to_list() == pytest.approx([-v for v in a[column].to_list()])
    assert a["five_attempts_diff"].abs().max() == 0  # every side's attempts are equal
    assert a["five_goals_diff"].abs().max() > 0  # type: ignore[operator]


def test_the_first_night_is_even() -> None:
    first = GAMES.filter(pl.col("game_date") == GAMES["game_date"].min())
    found = ga.columns_for(first, HISTORY, {m: 0.0 for m in ga.MEASURES})
    for column in ga.COLUMNS.values():
        assert (found[column] == 0).all()


def _flat_history(per_team: dict[str, float]) -> tuple[pl.DataFrame, pl.DataFrame]:
    """SYNTHETIC: two seasons of games among teams whose 5v5 goals for and against per game are
    each fixed at per_team, 48 minutes each: steady rates, not a league whose sides mirror."""
    teams = list(per_team)
    rows, games = [], []
    game_id = 0
    for season, year in ((20112012, 2011), (20122013, 2012)):
        for night in range(20):
            day = date(year, 10, 1 + night)
            public = datetime(year, 10, 2 + night, 10, tzinfo=UTC)
            for home, away in zip(teams[0::2], teams[1::2], strict=True):
                game_id += 1
                games.append(
                    {
                        "game_id": game_id,
                        "game_date": day,
                        "start_utc": datetime(year, 10, 1 + night, 23, tzinfo=UTC),
                    }
                )
                for team, other in ((home, away), (away, home)):
                    rows.append(
                        {
                            "game_id": game_id,
                            "season": season,
                            "game_date": day,
                            "team": team,
                            "opponent": other,
                            "gf": per_team[team],
                            "ga": per_team[team],
                            "cf": 0.0,
                            "ca": 0.0,
                            "xgf_5v5": 0.0,
                            "xga_5v5": 0.0,
                            "min_5v5": 48.0,
                            "observed_utc": public,
                        }
                    )
            teams = teams[1:] + teams[:1]
    history = pl.DataFrame(rows).with_columns(
        pl.col("game_id").cast(pl.Int64), pl.col("season").cast(pl.Int32)
    )
    return history, pl.DataFrame(games).with_columns(pl.col("game_id").cast(pl.Int64))


def test_a_league_of_equal_teams_takes_the_most_pull() -> None:
    history, games = _flat_history({t: 2.0 for t in ("BOS", "BUF", "DET", "FLA")})
    losses = ga.pull_losses(history, games, "goals", lines={})
    assert set(losses) == set(ga.PULLS) and max(losses.values()) < 1e-9
    assert ga.choose(losses) == max(ga.PULLS)


def test_steady_differences_between_teams_take_no_pull() -> None:
    history, games = _flat_history({"BOS": 4.0, "BUF": 1.0, "DET": 3.0, "FLA": 2.0})
    losses = ga.pull_losses(history, games, "goals", lines={})
    assert losses[0.0] < 1e-9 < losses[10.0] < losses[320.0]
    assert ga.choose(losses) == 0.0


def test_choose_breaks_ties_toward_more_pull() -> None:
    assert ga.choose({0.0: 2.0, 40.0: 1.0, 80.0: 1.0, 160.0: 3.0}) == 80.0


def _per_game(shifts: dict[str, float], seed: int = 1) -> pl.DataFrame:
    """SYNTHETIC per-game rows for each candidate: the baseline's moves, and each other
    candidate's the same plus shifts[name] and a little noise."""
    rng = np.random.default_rng(seed)
    days = [date(2013, 10, 1 + d % 28) for d in range(400)]
    seasons = [20132014 if i < 200 else 20142015 for i in range(400)]
    base = rng.normal(0, 1, 400)
    frames = []
    for name in ("xg part", *ga.FAMILY, "team xg"):
        move = base + shifts.get(name, 0.0) + rng.normal(0, 0.01, 400)
        frames.append(
            pl.DataFrame(
                {
                    "candidate": name,
                    "game_id": range(400),
                    "season": seasons,
                    "game_date": days,
                    "rested": [i % 3 != 0 for i in range(400)],
                    "move": move,
                    "result": rng.normal(0, 0.01, 400),
                }
            )
        )
    return pl.concat(frames)


def test_decide_keeps_only_a_candidate_whose_test_holds() -> None:
    found = ga.decide(_per_game({"goals": -0.05, "attempts": 0.05}))
    estimates = found["estimates"]
    assert found["family"] == list(ga.FAMILY)
    assert estimates["goals"]["holds"] and estimates["goals"]["kept"]
    # A significant estimate above 0 passes Holm but never holds; noise neither.
    assert not estimates["attempts"]["kept"] and not estimates["both"]["kept"]
    # The control is shown, outside the family.
    assert "threshold" not in estimates["team xg"] and "kept" not in estimates["team xg"]
    assert set(estimates["goals"]["per_season"]) == {"20132014", "20142015"}


def test_a_rating_at_or_after_the_prediction_is_refused() -> None:
    games = GAMES.filter(pl.col("season") == 20122013).head(3)
    as_of = games.select("game_id", t=ts.as_of(pl.col("game_date"), pl.col("start_utc")))
    frame = as_of.select("game_id", prediction_utc="t", season=pl.lit(20122013))
    read = cast(ys.Read, SimpleNamespace(games=games, frame=frame))
    with pytest.raises(ValueError, match="3 games rated at or after"):
        ga.with_rates(read, HISTORY, {m: 10.0 for m in ga.MEASURES})


def test_the_report_names_the_pulls_the_check_and_the_decision() -> None:
    read = grf.league_read()
    rng = np.random.default_rng(7)
    frame = read.frame.with_columns(
        pl.Series(column, rng.normal(0, 0.1, read.frame.height)) for column in ga.COLUMNS.values()
    )
    per_game, figures = ys.run(frame, "explore", ga.CANDIDATES)
    found = {
        "pulls": {
            "losses": {m: {str(p): 100.0 - p / 10 for p in ga.PULLS} for m in ga.MEASURES},
            "chosen": {m: 320.0 for m in ga.MEASURES},
        },
        "check": {"games": 10, "agree": 10, "largest_gap": 0.0, "settings": ts.TUNED.label},
        "tuning_cutoff": str(ts.TUNED_CUTOFF),
        "figures": figures,
        "calibration": ga.calibration(frame),
        "correlations": ga.correlations(frame),
        "decision": ga.decide(per_game),
    }
    text = ga.markdown(found, "goals-attempts-20261111-abcdef0")
    for heading in ("## The pulls, chosen on 2012-13", "## The candidates", "## The decision"):
        assert heading in text
    assert "on 10 of 10" in text and "**68.0**" in text
    assert "| team xg |" in text and "five_goals_diff" in text
