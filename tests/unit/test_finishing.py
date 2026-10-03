"""Finishing φ and goalie conversion (#105, ADR 0022)."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import finishing_fixtures as ff
import numpy as np
import polars as pl
import pytest

from nhl_edge.audit import finishing as report
from nhl_edge.ratings import decayed
from nhl_edge.ratings import finishing as fn

UTC_TYPE = pl.Datetime("us", "UTC")


def games_of(days: list[date], season: int = 20122013) -> pl.DataFrame:
    first = season // 10000 * 1_000_000 + 20_001
    return pl.DataFrame(
        {
            "game_id": list(range(first, first + len(days))),
            "season": [season] * len(days),
            "game_date": days,
            "home": ["BOS"] * len(days),
            "away": ["TOR"] * len(days),
            "start_utc": [
                datetime.combine(d, datetime.min.time(), UTC) + timedelta(hours=23) for d in days
            ],
        },
        schema_overrides={"season": pl.Int32},
    )


def test_a_skater_game_sums_his_shots_with_xg() -> None:
    day = date(2012, 10, 10)
    games = games_of([day])
    gid = games["game_id"][0]
    public = datetime(2012, 10, 11, 10, tzinfo=UTC)
    minutes = pl.DataFrame(
        {
            "game_id": [gid, gid],
            "season": [20122013, 20122013],
            "game_date": [day, day],
            "team": ["BOS", "TOR"],
            "player_id": [10, 20],
            "role": ["F", "D"],
            "5v5": [12.0, 18.0],
            "pp": [2.0, 0.0],
            "pk": [1.0, 3.0],
            "observed_utc": [public, public],
        },
        schema_overrides={"season": pl.Int32, "observed_utc": UTC_TYPE},
    )
    shots = pl.DataFrame(
        {
            "game_id": [gid] * 4,
            "event_id": [1, 2, 3, 4],
            "team": ["BOS", "BOS", "TOR", "BOS"],
            # 99 shot for BOS without dressing: left out.
            "shooter_id": [10, 10, 20, 99],
            "is_goal": [True, False, False, True],
            "observed_utc": [public] * 4,
        },
        schema_overrides={"observed_utc": UTC_TYPE},
    )
    shot_xg = pl.DataFrame(
        {
            "game_id": [gid] * 4,
            "event_id": [1, 2, 3, 4],
            "xg": [0.1, 0.3, 0.05, 0.2],
            # The xG public later than the shots.
            "observed_utc": [public + timedelta(hours=1)] * 4,
        },
        schema_overrides={"observed_utc": UTC_TYPE},
    )
    rows = fn.shooter_games(minutes, shots, shot_xg, games).sort("player_id")
    assert rows["player_id"].to_list() == [10, 20]
    assert rows["xg"].to_list() == pytest.approx([0.4, 0.05])
    assert rows["xg_sq"].to_list() == pytest.approx([0.1, 0.0025])
    assert rows["goals"].to_list() == [1.0, 0.0]
    assert rows["hours"].to_list() == pytest.approx([15 / 60, 21 / 60])
    assert (rows["observed_utc"] == public + timedelta(hours=1)).all()


def shooter_rows(players: dict[int, tuple[str, float, float, float]], season: int) -> pl.DataFrame:
    """shooter_games rows of one season for {id: (role, hours, xg, goals)}, spread over 25
    games."""
    out = []
    for player, (role, hours, xg, goals) in players.items():
        for k in range(25):
            day = date(season // 10000, 10, 10) + timedelta(days=k)
            out.append(
                {
                    "game_id": season // 10000 * 1_000_000 + 20_001 + k,
                    "season": season,
                    "game_date": day,
                    "team": "BOS",
                    "player_id": player,
                    "role": role,
                    "hours": hours / 25,
                    "xg": xg / 25,
                    # Shots of 0.1 xG each.
                    "xg_sq": 0.1 * xg / 25,
                    "goals": goals / 25,
                    "day": float(k),
                    "observed_utc": datetime.combine(day, datetime.min.time(), UTC)
                    + timedelta(hours=34),
                }
            )
    return pl.DataFrame(out, schema_overrides={"season": pl.Int32})


def test_the_first_season_has_no_pulls() -> None:
    rows = shooter_rows({1: ("F", 10.0, 5.0, 5.0)}, 20112012)
    pulls = fn.season_pulls(rows, 20112012, games_of([date(2011, 10, 8)], 20112012))
    assert np.isinf(pulls.finishing["F"]) and np.isinf(pulls.rate["D"])
    assert pulls.cutoff is None


def test_the_finishing_pull_is_the_noise_over_the_spread_of_true_finishing() -> None:
    rng = np.random.default_rng(2)
    # True finishing with mean 1 and variance 0.04; shots of 0.1 xG, so a goal's noise is 0.9 of
    # a Poisson count's: a pull of 0.9 / 0.04 = 22.5 expected goals.
    players = {}
    for i in range(3000):
        phi = rng.gamma(25, 1 / 25)
        players[i] = ("F", 15.0, 60.0, float(rng.binomial(600, 0.1 * phi)))
    players[9001] = ("D", 15.0, 3.0, 3.0)
    players[9002] = ("D", 15.0, 3.0, 2.0)
    rows = shooter_rows(players, 20112012)
    pulls = fn.season_pulls(rows, 20122013, games_of([date(2012, 10, 8)]))
    assert pulls.finishing["F"] == pytest.approx(22.5, rel=0.25)
    # Every forward's xG rate is exactly 4 per hour: nothing to tell apart.
    assert np.isinf(pulls.rate["F"])
    assert pulls.cutoff == rows["observed_utc"].max()


def test_the_pulls_refuse_earlier_games_public_after_the_season_starts() -> None:
    rows = shooter_rows(
        {
            1: ("F", 10.0, 5.0, 4.0),
            2: ("F", 10.0, 6.0, 7.0),
            3: ("D", 10.0, 1.0, 1.0),
            4: ("D", 10.0, 2.0, 1.0),
        },
        20112012,
    ).with_columns(observed_utc=pl.lit(datetime(2012, 10, 9, tzinfo=UTC), UTC_TYPE))
    with pytest.raises(ValueError, match="not before"):
        fn.season_pulls(rows, 20122013, games_of([date(2012, 10, 8)]))


def test_phi_is_his_record_against_the_league_s_finishing_pulled_toward_1() -> None:
    # Two games, days 0 and 1: player 1 scores 3 on 2 xG, player 2 scores 1 on 2 xG.
    rows = pl.concat(
        [
            shooter_rows({1: ("F", 1.0, 1.0, 1.5), 2: ("F", 1.0, 1.0, 0.5)}, 20112012).filter(
                pl.col("day") < 2
            )
        ]
    ).with_columns(
        hours=pl.lit(0.5),
        xg=pl.lit(1.0),
        goals=pl.when(pl.col("player_id") == 1).then(1.5).otherwise(0.5),
    )
    pull = fn.SeasonPulls(20122013, {"F": 4.0, "D": 4.0}, {"F": 2.0, "D": 2.0}, None)
    as_of = datetime(2012, 10, 8, 14, tzinfo=UTC)
    candidates = pl.DataFrame(
        {
            "game_id": [2012020001] * 2,
            "season": [20122013] * 2,
            "game_date": [date(2012, 10, 8)] * 2,
            "team": ["BOS"] * 2,
            "player_id": [1, 7],
            "role": ["F", "F"],
            "as_of_utc": [as_of] * 2,
        },
        schema_overrides={"season": pl.Int32, "as_of_utc": UTC_TYPE},
    )
    rated = {
        r["player_id"]: r
        for r in fn.rates(candidates, rows, {20122013: pull}).iter_rows(named=True)
    }
    w = np.array([0.5 ** (1 / fn.HALF_LIFE_DAYS), 1.0])
    goals, xg, hours = 1.5 * w.sum(), w.sum(), 0.5 * w.sum()
    kappa = 1.0  # 4 goals on 4 xG across the league
    assert rated[1]["league_finishing"] == pytest.approx(kappa)
    assert rated[1]["phi"] == pytest.approx((goals + 4) / (kappa * xg + 4))
    assert rated[1]["phi_sd"] == pytest.approx(np.sqrt(goals + 4) / (kappa * xg + 4))
    rho = 2.0  # 1 xG per half hour
    assert rated[1]["xg_rate"] == pytest.approx((xg + 2 * rho) / (hours + 2))
    # Without shots, a candidate finishes like the league and shoots at his role's rate.
    assert rated[7]["phi"] == pytest.approx(1.0) and rated[7]["xg_rate"] == pytest.approx(rho)


def test_a_team_s_phi_weights_its_shooters_by_their_expected_xg_and_gamma_follows_the_goalie() -> (
    None
):
    as_of = datetime(2012, 10, 8, 14, tzinfo=UTC)
    games = games_of([date(2012, 10, 8)])
    gid = games["game_id"][0]
    cutoff = datetime(2012, 6, 1, tzinfo=UTC)
    rated = pl.DataFrame(
        {
            "game_id": [gid] * 3,
            "season": [20122013] * 3,
            "team": ["BOS", "BOS", "TOR"],
            "player_id": [10, 11, 20],
            # BOS: a sniper shooting twice as often as his teammate.
            "phi": [1.2, 0.9, 1.0],
            "xg_rate": [1.0, 0.5, 0.8],
            "known_utc": [as_of - timedelta(hours=5)] * 3,
        },
        schema_overrides={"season": pl.Int32, "known_utc": UTC_TYPE},
    )
    candidates = pl.DataFrame(
        {
            "game_id": [gid] * 3,
            "team": ["BOS", "BOS", "TOR"],
            "player_id": [10, 11, 20],
            "exp_5v5": [10.0, 10.0, 10.0],
            "exp_pp": [0.0, 0.0, 0.0],
            "exp_pk": [0.0, 0.0, 0.0],
            "train_cutoff": [cutoff] * 3,
        },
        schema_overrides={"train_cutoff": UTC_TYPE},
    )
    # BOS has a replacement forward with 10 minutes at the forwards' 0.5 xG per hour.
    replacements = pl.DataFrame(
        {
            "game_id": [gid],
            "team": ["BOS"],
            "role": ["F"],
            "exp_5v5": [10.0],
            "exp_pp": [0.0],
            "exp_pk": [0.0],
            "train_cutoff": [cutoff],
        },
        schema_overrides={"train_cutoff": UTC_TYPE},
    )
    roles = pl.DataFrame(
        {"as_of_utc": [as_of], "league_finishing": [1.02], "rho_F": [0.5], "rho_D": [0.2]},
        schema_overrides={"as_of_utc": UTC_TYPE},
    )
    effects = pl.DataFrame(
        {
            "game_id": [gid, gid],
            "team": ["TOR", "TOR"],
            "goalie_id": [1, 2],
            "effect": [0.006, -0.003],
            "train_cutoff": [cutoff, cutoff + timedelta(days=1)],
        },
        schema_overrides={"train_cutoff": UTC_TYPE},
    )
    figures = pl.DataFrame(
        {
            "season": [20122013],
            "as_of_utc": [as_of],
            "xg_per_shot": [0.06],
            "known_utc": [as_of - timedelta(hours=4)],
        },
        schema_overrides={"season": pl.Int32, "as_of_utc": UTC_TYPE, "known_utc": UTC_TYPE},
    )
    shared, rows = fn.multipliers(rated, candidates, replacements, roles, effects, figures, games)
    # Weights 10, 5 and the replacement's 5: shares one half, a quarter and a quarter.
    shares = dict(zip(shared["player_id"], shared["share"], strict=True))
    assert shares[10] == pytest.approx(0.5) and shares[11] == pytest.approx(0.25)
    bos = rows.filter(pl.col("team") == "BOS").sort("goalie_id")
    phi = 0.5 * 1.2 + 0.25 * 0.9 + 0.25 * 1.0
    assert bos["phi"].to_list() == pytest.approx([phi, phi])
    assert bos["gamma"].to_list() == pytest.approx([1 - 0.006 / 0.06, 1 + 0.003 / 0.06])
    # The league finishes at 1.02 times its xG: B3's factor carries it.
    assert bos["multiplier"].to_list() == pytest.approx([1.02 * phi * 0.9, 1.02 * phi * 1.05])
    assert bos["known_utc"][0] == as_of - timedelta(hours=4)
    assert bos["effect_cutoff"].to_list() == [cutoff, cutoff + timedelta(days=1)]
    # BOS has no candidate goalie here, as in a new team's first game: TOR gets one row without
    # a goalie, at gamma 1.
    (tor,) = rows.filter(pl.col("team") == "TOR").iter_rows(named=True)
    assert tor["goalie_id"] is None and tor["gamma"] == 1.0
    assert tor["multiplier"] == pytest.approx(1.02 * tor["phi"])


def test_shot_figures_read_the_season_and_the_one_before_public_before_the_as_of_time() -> None:
    as_of = datetime(2013, 1, 10, 15, tzinfo=UTC)

    def game(
        gid: int, season: int, observed: datetime, xgs: list[float]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        shots = [{"game_id": gid, "event_id": k, "observed_utc": observed} for k in range(len(xgs))]
        values = [
            {"game_id": gid, "season": season, "event_id": k, "xg": x, "observed_utc": observed}
            for k, x in enumerate(xgs)
        ]
        return shots, values

    parts = [
        game(1, 20102011, datetime(2011, 1, 1, tzinfo=UTC), [0.9, 0.9]),
        game(2, 20112012, datetime(2012, 1, 1, tzinfo=UTC), [0.1, 0.2]),
        game(3, 20122013, datetime(2013, 1, 1, tzinfo=UTC), [0.3]),
        game(4, 20122013, as_of, [0.9]),
    ]
    shots = pl.DataFrame(
        [s for p in parts for s in p[0]], schema_overrides={"observed_utc": UTC_TYPE}
    )
    shot_xg = pl.DataFrame(
        [v for p in parts for v in p[1]],
        schema_overrides={"season": pl.Int32, "observed_utc": UTC_TYPE},
    )
    times = pl.DataFrame(
        {"season": [20122013], "as_of_utc": [as_of]},
        schema_overrides={"season": pl.Int32, "as_of_utc": UTC_TYPE},
    )
    (row,) = fn.shot_figures(shots, shot_xg, times).iter_rows(named=True)
    assert row["xg_per_shot"] == pytest.approx(0.6 / 3)
    assert row["known_utc"] == datetime(2013, 1, 1, tzinfo=UTC)


def test_the_moments_pull_takes_the_noise_out() -> None:
    exposure = np.full(4, 10.0)
    # Rates 0.5, 0.5, 1.5, 1.5 around 1: a spread of 0.25, less 0.1 of Poisson noise.
    counts = np.array([5.0, 5.0, 15.0, 15.0])
    assert decayed.moments_pull(counts, exposure) == pytest.approx(1 / (0.25 - 0.1))
    assert decayed.moments_pull(counts, exposure, noise=0.5) == pytest.approx(0.5 / (0.25 - 0.05))
    assert np.isinf(decayed.moments_pull(np.full(4, 10.0), exposure))


def test_scoring_mixes_the_opposing_goalies_by_their_start_probabilities() -> None:
    multipliers = pl.DataFrame(
        {
            "game_id": [1, 1, 1],
            "season": [20122013] * 3,
            "game_date": [date(2012, 10, 8)] * 3,
            "team": ["BOS", "BOS", "TOR"],
            "opponent": ["TOR", "TOR", "BOS"],
            "goalie_id": [1, 2, 3],
            "phi": [1.1, 1.1, 1.0],
            "gamma": [0.9, 1.1, 1.0],
            "league_finishing": [1.0, 1.0, 1.0],
        },
        schema_overrides={"season": pl.Int32},
    )
    goals = pl.DataFrame(
        {"game_id": [1, 1], "team": ["BOS", "TOR"], "xg": [3.0, 2.0], "goals": [3.0, 1.0]}
    )
    starts = pl.DataFrame(
        {
            "game_id": [1, 1, 1],
            "team": ["TOR", "TOR", "BOS"],
            "goalie_id": [1, 2, 3],
            "p_start": [0.75, 0.25, 1.0],
        }
    )
    frame = report.scored(multipliers, goals, starts)
    bos = frame.filter(pl.col("team") == "BOS").row(0, named=True)
    gamma = 0.75 * 0.9 + 0.25 * 1.1
    assert bos["gamma"] == pytest.approx(gamma)
    assert bos["error"] == pytest.approx(0.0)
    assert bos["difference_both"] == pytest.approx((3.0 * 1.1 * gamma - 3.0) ** 2)
    assert frame.height == 2


def test_scoring_keeps_games_before_league_finishing_and_without_goalie_candidates() -> None:
    multipliers = pl.DataFrame(
        {
            "game_id": [1, 1],
            "season": [20112012] * 2,
            "game_date": [date(2011, 10, 6)] * 2,
            "team": ["BOS", "TOR"],
            "opponent": ["TOR", "BOS"],
            # TOR has no candidate goalie, and no league finishing is public yet.
            "goalie_id": [None, 3],
            "phi": [1.0, 1.0],
            "gamma": [1.0, 0.95],
            "league_finishing": [None, None],
        },
        schema_overrides={
            "season": pl.Int32,
            "goalie_id": pl.Int64,
            "league_finishing": pl.Float64,
        },
    )
    goals = pl.DataFrame(
        {"game_id": [1, 1], "team": ["BOS", "TOR"], "xg": [2.0, 3.0], "goals": [2.0, 3.0]}
    )
    starts = pl.DataFrame({"game_id": [1], "team": ["BOS"], "goalie_id": [3], "p_start": [1.0]})
    frame = report.scored(multipliers, goals, starts).sort("team")
    assert frame["team"].to_list() == ["BOS", "TOR"]
    assert frame["reference"].to_list() == [2.0, 3.0]
    assert frame["gamma"].to_list() == pytest.approx([1.0, 0.95])


def test_the_inputs_must_cover_every_game_with_shots_and_xg() -> None:
    games = games_of([date(2012, 10, 8), date(2012, 10, 9)])
    shots = pl.DataFrame({"game_id": games["game_id"].to_list()})
    shot_xg = pl.DataFrame({"game_id": games["game_id"].head(1).to_list()})
    (problem,) = fn.input_problems(games, shots, shot_xg, 20122013)
    assert problem.startswith("20122013: 1 games without xG")
    assert fn.input_problems(games, shots, shots, 20122013) == []


def fixture_lake(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from nhl_edge.lake.tables import Lake

    frames = ff.league()

    def read(self: Lake, table: str, seasons: Any = None) -> pl.DataFrame:
        frame = frames[table]
        return frame if seasons is None else frame.filter(pl.col("season").is_in(list(seasons)))

    monkeypatch.setattr(Lake, "read", read)
    return frames


def test_the_command_writes_both_tables_and_the_report(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app

    fixture_lake(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app, ["finishing", "--seasons", "20112012-20122013", "--out", "out"]
    )
    assert result.exit_code == 0, result.output
    (path,) = (tmp_path / "out").glob("finishing-*.md")
    assert "# Finishing and goalie conversion" in path.read_text()
    lake = tmp_path / "data" / "lake"
    finishing = pl.read_parquet(lake / "finishing" / "**" / "*.parquet")
    multipliers = pl.read_parquet(lake / "goal_multipliers" / "**" / "*.parquet")
    # Two candidate goalies of the opponent for each of 120 games' two teams.
    assert multipliers.height == 120 * 2 * 2
    # 2011-12 has no earlier season: everyone at 1.
    assert (finishing.filter(pl.col("season") == ff.SEASONS[0])["phi"] == 1.0).all()
    # The sniper ends 2012-13 the best finisher, and the good goalies hold their shooters down.
    last = finishing.filter(pl.col("game_date") == finishing["game_date"].max())
    assert last.sort("phi")["player_id"][-1] == ff.SNIPER
    good = [ff.goalies(t)[0] for t in ("BOS", "BUF", "DET", "TOR")]
    late = multipliers.filter(pl.col("season") == ff.SEASONS[1])
    assert (late.filter(pl.col("goalie_id").is_in(good))["gamma"] < 1).all()


def test_the_command_refuses_a_season_before_2011_12(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app

    fixture_lake(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["finishing", "--seasons", "20102011"])
    assert result.exit_code == 2
