"""Penalty rates and expected power plays (#104, ADR 0021)."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import penalty_fixtures as pf
import polars as pl
import pytest

from nhl_edge.features import team_strength as ts
from nhl_edge.ratings import penalty_rates as pr

UTC_TYPE = pl.Datetime("us", "UTC")
DAY = date(2011, 10, 10)
PUBLIC = datetime(2011, 10, 11, 10, tzinfo=UTC)


def penalties(rows: list[tuple[Any, ...]]) -> pl.DataFrame:
    """Penalties rows from (game_id, period, seconds, team, is_home, committed_by, drawn_by,
    duration_min)."""
    return pl.DataFrame(
        rows,
        schema=[
            "game_id",
            "period",
            "seconds",
            "team",
            "is_home",
            "committed_by",
            "drawn_by",
            "duration_min",
        ],
        orient="row",
    ).with_columns(
        season=pl.lit(20112012, pl.Int32),
        game_date=pl.lit(DAY),
        observed_utc=pl.lit(PUBLIC, UTC_TYPE),
        committed_by=pl.col("committed_by").cast(pl.Int64),
        drawn_by=pl.col("drawn_by").cast(pl.Int64),
    )


def test_offset_penalties_weigh_nothing() -> None:
    weighted = pr.unoffset(
        penalties(
            [
                # Coincidental minors: no power play.
                (1, 1, 100, "BOS", True, 10, 20, 2),
                (1, 1, 100, "TOR", False, 20, 10, 2),
                # Two minors against one: the two leave a power play between them.
                (1, 2, 50, "BOS", True, 11, 21, 2),
                (1, 2, 50, "BOS", True, 12, 22, 2),
                (1, 2, 50, "TOR", False, 22, 12, 2),
                # A major against a minor is not netted.
                (1, 3, 10, "BOS", True, 13, 23, 5),
                (1, 3, 10, "TOR", False, 23, 13, 2),
                # A misconduct and a penalty shot never count.
                (1, 3, 500, "BOS", True, 14, None, 10),
                (1, 3, 600, "TOR", False, 24, 14, 0),
            ]
        )
    ).sort("committed_by")
    assert weighted["committed_by"].to_list() == [10, 11, 12, 13, 20, 22, 23]
    assert weighted["weight"].to_list() == [0.0, 0.5, 0.5, 1.0, 0.0, 0.0, 1.0]


def minutes_rows(rows: list[tuple[Any, ...]]) -> pl.DataFrame:
    """player_minutes rows from (game_id, game_date, team, player_id, role, 5v5, pp, pk)."""
    return pl.DataFrame(
        rows,
        schema=["game_id", "game_date", "team", "player_id", "role", "5v5", "pp", "pk"],
        orient="row",
    ).with_columns(
        season=pl.lit(20112012, pl.Int32),
        observed_utc=(
            pl.col("game_date").cast(pl.Datetime("us")) + timedelta(hours=34)
        ).dt.replace_time_zone("UTC"),
        **{c: pl.col(c).cast(pl.Float64) for c in ("5v5", "pp", "pk")},
    )


def games_of(days: list[date]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "game_id": list(range(2011020001, 2011020001 + len(days))),
            "season": [20112012] * len(days),
            "game_date": days,
            "home": ["BOS"] * len(days),
            "away": ["TOR"] * len(days),
            "start_utc": [
                datetime.combine(d, datetime.min.time(), UTC) + timedelta(hours=23) for d in days
            ],
        },
        schema_overrides={"season": pl.Int32},
    )


def test_a_penalty_is_taken_by_its_committer_and_drawn_by_the_other_team_s_skater() -> None:
    games = games_of([DAY])
    gid = games["game_id"][0]
    minutes = minutes_rows(
        [
            (gid, DAY, "BOS", 10, "F", 12.0, 2.0, 1.0),
            (gid, DAY, "TOR", 20, "D", 18.0, 0.0, 3.0),
        ]
    )
    weighted = pr.unoffset(
        penalties(
            [
                (gid, 1, 100, "BOS", True, 10, 20, 2),
                # A drawer on the penalized team is not a drawer.
                (gid, 1, 300, "TOR", False, 20, 21, 2),
                (gid, 2, 300, "BOS", True, 11, 10, 2),
                # A game without stints adds nothing.
                (gid + 1, 1, 100, "BOS", True, 10, 20, 2),
            ]
        )
    )
    rows = pr.player_games(minutes, weighted, games).sort("player_id")
    assert rows["player_id"].to_list() == [10, 20]
    assert rows["hours"].to_list() == pytest.approx([15 / 60, 21 / 60])
    assert rows["taken"].to_list() == [1.0, 1.0]
    assert rows["drawn"].to_list() == [0.0, 1.0]


def history_rows(
    players: dict[int, tuple[str, float, float, float]], games: int = 25
) -> pl.DataFrame:
    """player_games rows for players {id: (role, hours per game, taken per game, drawn per
    game)} over a season of games, one game a day."""
    out = []
    for player, (role, hours, taken, drawn) in players.items():
        for k in range(games):
            day = date(2010, 10, 10) + timedelta(days=k)
            out.append(
                {
                    "game_id": 2010020001 + k,
                    "season": 20102011,
                    "game_date": day,
                    "team": "BOS",
                    "player_id": player,
                    "role": role,
                    "hours": hours,
                    "taken": taken,
                    "drawn": drawn,
                    "day": float(k),
                    "observed_utc": datetime.combine(day, datetime.min.time(), UTC)
                    + timedelta(hours=34),
                }
            )
    return pl.DataFrame(out, schema_overrides={"season": pl.Int32})


def next_season() -> pl.DataFrame:
    return games_of([date(2011, 10, 8)])


def test_the_pull_is_the_role_rate_over_the_spread_of_true_rates() -> None:
    rng = np.random.default_rng(1)
    # True rates per hour with mean 0.8 and variance 0.04: a pull of 0.8 / 0.04 = 20 hours.
    shape, scale = 0.8**2 / 0.04, 0.04 / 0.8
    players = {}
    for i in range(3000):
        rate = rng.gamma(shape, scale)
        # 30 hours each, so Poisson noise doesn't swamp the spread.
        players[i] = ("F", 1.2, rng.poisson(rate * 1.2 * 25) / 25, 0.24)
    players[9001] = ("D", 0.3, 0.2, 0.1)
    players[9002] = ("D", 0.3, 0.25, 0.1)
    pulls = pr.season_pulls(history_rows(players), 20112012, next_season())
    assert pulls.pull["F", pr.TAKEN] == pytest.approx(20.0, rel=0.25)
    # Every forward draws at exactly the same rate: nothing to tell apart, so the pull is
    # infinite.
    assert np.isinf(pulls.pull["F", pr.DRAWN])
    assert pulls.source == 20102011


def test_the_pulls_refuse_a_season_before_public_after_the_season_starts() -> None:
    rows = history_rows(
        {
            1: ("F", 0.3, 0.2, 0.3),
            2: ("F", 0.3, 0.3, 0.2),
            3: ("D", 0.3, 0.2, 0.1),
            4: ("D", 0.3, 0.1, 0.2),
        }
    )
    late = rows.with_columns(observed_utc=pl.lit(datetime(2011, 10, 9, tzinfo=UTC), UTC_TYPE))
    with pytest.raises(ValueError, match="not before"):
        pr.season_pulls(late, 20112012, next_season())


def test_a_rate_is_his_decayed_record_pulled_toward_his_role() -> None:
    rows = history_rows(
        {
            1: ("F", 0.3, 0.2, 0.3),
            2: ("F", 0.3, 0.6, 0.1),
            3: ("D", 0.3, 0.2, 0.1),
            4: ("D", 0.3, 0.1, 0.2),
        },
        games=2,
    )
    pull = pr.SeasonPulls(
        20112012,
        20102011,
        {(r, c): 5.0 for r in ("F", "D") for c in pr.COMPONENTS},
        datetime(2010, 10, 12, 10, tzinfo=UTC),
    )
    as_of = datetime(2011, 10, 8, 14, tzinfo=UTC)
    candidates = pl.DataFrame(
        {
            "game_id": [2011020001, 2011020001],
            "season": [20112012, 20112012],
            "game_date": [date(2011, 10, 8)] * 2,
            "team": ["BOS", "BOS"],
            "player_id": [1, 7],
            "role": ["F", "F"],
            "as_of_utc": [as_of, as_of],
        },
        schema_overrides={"season": pl.Int32, "as_of_utc": UTC_TYPE},
    )
    rated = pr.rates(candidates, rows, {20112012: pull})
    # Two games, days 0 and 1, decayed to day 1, the latest read.
    weights = np.array([0.5 ** (1 / pr.HALF_LIFE_DAYS), 1.0])
    hours = 0.3 * weights.sum()
    rho = (0.2 + 0.6) / 0.6  # the forwards' taken rate: both forwards weigh the same
    taken = rated.filter(pl.col("player_id") == 1, pl.col("component") == pr.TAKEN).row(
        0, named=True
    )
    assert taken["hours"] == pytest.approx(hours)
    assert taken["prior"] == pytest.approx(rho)
    assert taken["mean"] == pytest.approx((0.2 * weights.sum() + 5 * rho) / (hours + 5))
    assert taken["sd"] == pytest.approx(np.sqrt(0.2 * weights.sum() + 5 * rho) / (hours + 5))
    # A candidate without games sits at his role's rate.
    newcomer = rated.filter(pl.col("player_id") == 7, pl.col("component") == pr.TAKEN).row(
        0, named=True
    )
    assert newcomer["mean"] == pytest.approx(rho) and newcomer["hours"] == 0.0
    assert (rated["known_utc"] < rated["as_of_utc"]).all()


def test_an_infinite_pull_puts_everyone_at_the_role_rate() -> None:
    rows = history_rows(
        {1: ("F", 0.3, 0.2, 0.3), 2: ("F", 0.3, 0.6, 0.1), 3: ("D", 0.3, 0.2, 0.1)}, games=2
    )
    pull = pr.SeasonPulls(
        20112012,
        20102011,
        {(r, c): np.inf for r in ("F", "D") for c in pr.COMPONENTS},
        datetime(2010, 10, 12, 10, tzinfo=UTC),
    )
    candidates = pl.DataFrame(
        {
            "game_id": [2011020001],
            "season": [20112012],
            "game_date": [date(2011, 10, 8)],
            "team": ["BOS"],
            "player_id": [2],
            "role": ["F"],
            "as_of_utc": [datetime(2011, 10, 8, 14, tzinfo=UTC)],
        },
        schema_overrides={"season": pl.Int32, "as_of_utc": UTC_TYPE},
    )
    rated = pr.rates(candidates, rows, {20112012: pull})
    assert rated["mean"].to_list() == pytest.approx(rated["prior"].to_list())
    assert rated["sd"].to_list() == [0.0, 0.0]


def test_expected_power_plays_average_the_two_views_at_the_league_s_level() -> None:
    as_of = datetime(2011, 10, 8, 14, tzinfo=UTC)
    games = games_of([date(2011, 10, 8)])
    gid = games["game_id"][0]
    rated = pl.DataFrame(
        {
            "game_id": [gid] * 4,
            "season": [20112012] * 4,
            "team": ["BOS", "BOS", "TOR", "TOR"],
            "player_id": [10, 10, 20, 20],
            "component": [pr.TAKEN, pr.DRAWN] * 2,
            # BOS takes and draws at twice its role's rate; TOR at its role's.
            "mean": [2.0, 1.0, 1.0, 0.5],
            "prior": [1.0, 0.5, 1.0, 0.5],
            "known_utc": [as_of - timedelta(hours=5)] * 4,
        },
        schema_overrides={"season": pl.Int32, "known_utc": UTC_TYPE},
    )
    lineup_cutoff = datetime(2011, 6, 1, tzinfo=UTC)
    candidates = pl.DataFrame(
        {
            "game_id": [gid, gid],
            "team": ["BOS", "TOR"],
            "player_id": [10, 20],
            "exp_5v5": [10.0, 10.0],
            "exp_pp": [1.0, 1.0],
            "exp_pk": [1.0, 1.0],
            "train_cutoff": [lineup_cutoff, lineup_cutoff],
        },
        schema_overrides={"train_cutoff": UTC_TYPE},
    )
    # Each team has a replacement forward with 12 minutes at his role's rates.
    replacements = pl.DataFrame(
        {
            "game_id": [gid, gid],
            "team": ["BOS", "TOR"],
            "role": ["F", "F"],
            "exp_5v5": [10.0, 10.0],
            "exp_pp": [1.0, 1.0],
            "exp_pk": [1.0, 1.0],
            "train_cutoff": [lineup_cutoff, lineup_cutoff + timedelta(days=1)],
        },
        schema_overrides={"train_cutoff": UTC_TYPE},
    )
    roles = pl.DataFrame(
        {
            "as_of_utc": [as_of, as_of],
            "role": ["F", "D"],
            f"rho_{pr.TAKEN}": [1.0, 1.0],
            f"rho_{pr.DRAWN}": [0.5, 0.5],
        },
        schema_overrides={"as_of_utc": UTC_TYPE},
    )
    league = pl.DataFrame(
        {
            "season": [20112012],
            "as_of_utc": [as_of],
            "league_opportunities": [3.0],
            "pp_length": [1.8],
            "sh_xg_per_pk_minute": [0.01],
            "known_utc": [as_of - timedelta(hours=4)],
        },
        schema_overrides={"season": pl.Int32, "as_of_utc": UTC_TYPE, "known_utc": UTC_TYPE},
    )
    got = {
        r["team"]: r
        for r in pr.expected(rated, candidates, replacements, roles, league, games).iter_rows(
            named=True
        )
    }
    # BOS's lineup: half its minutes at twice the rate, half at the role's: an index of 1.5.
    assert got["BOS"]["taken_index"] == pytest.approx(1.0)  # TOR's lineup takes at its rate
    assert got["BOS"]["drawn_index"] == pytest.approx(1.5)
    assert got["TOR"]["taken_index"] == pytest.approx(1.5)
    assert got["TOR"]["drawn_index"] == pytest.approx(1.0)
    assert got["BOS"]["opportunities"] == pytest.approx(3.0 * (1.0 + 1.5) / 2)
    assert got["BOS"]["pp_minutes"] == pytest.approx(3.0 * 1.25 * 1.8)
    assert got["BOS"]["pk_minutes"] == pytest.approx(got["TOR"]["pp_minutes"])
    assert got["BOS"]["sh_xg"] == pytest.approx(0.01 * got["TOR"]["pp_minutes"])
    assert got["BOS"]["is_home"] and not got["TOR"]["is_home"]
    # The latest of the league's and the rates' reads, and the later lineup cutoff of the game.
    assert got["BOS"]["known_utc"] == as_of - timedelta(hours=4)
    assert got["BOS"]["lineup_cutoff"] == lineup_cutoff + timedelta(days=1)


def team_game(
    season: int, observed: datetime, opportunities: float, pp: float, pk: float, sh: float | None
) -> dict[str, Any]:
    return {
        "season": season,
        "observed_utc": observed,
        "opportunities": opportunities,
        "pp_minutes": pp,
        "pk_minutes": pk,
        "sh_xg": sh,
        "has_xg": sh is not None,
    }


def test_league_figures_read_the_season_and_the_one_before_public_before_the_as_of_time() -> None:
    as_of = datetime(2012, 1, 10, 15, tzinfo=UTC)
    history = pl.DataFrame(
        [
            # Two seasons back: not read.
            team_game(20092010, datetime(2010, 1, 1, tzinfo=UTC), 9.0, 9.0, 9.0, None),
            # The season before, without xG.
            team_game(
                20102011,
                datetime(2011, 3, 1, tzinfo=UTC),
                4.0,
                8.0,
                6.0,
                None,
            ),
            team_game(20112012, datetime(2012, 1, 1, tzinfo=UTC), 2.0, 3.0, 4.0, 0.08),
            # Public exactly at the as-of time: not read.
            team_game(20112012, as_of, 7.0, 7.0, 7.0, 0.7),
        ],
        schema_overrides={"season": pl.Int32, "observed_utc": UTC_TYPE},
    )
    times = pl.DataFrame(
        {"season": [20112012], "as_of_utc": [as_of]},
        schema_overrides={"season": pl.Int32, "as_of_utc": UTC_TYPE},
    )
    (row,) = pr.league_figures(history, times).iter_rows(named=True)
    assert row["league_opportunities"] == pytest.approx((4.0 + 2.0) / 2)
    assert row["pp_length"] == pytest.approx((8.0 + 3.0) / 6.0)
    assert row["sh_xg_per_pk_minute"] == pytest.approx(0.08 / 4.0)
    assert row["known_utc"] == datetime(2012, 1, 1, tzinfo=UTC)


def test_b2_s_power_play_minutes_average_the_team_s_and_the_opponent_s() -> None:
    frames = pf.league()
    games = frames["games"].filter(pl.col("season") == pf.SEASONS[1])
    history = ts.team_games(frames["shots"], frames["shot_xg"], frames["strength_time"])
    minutes = ts.power_play_minutes(games, history, ts.TUNED)
    assert minutes.height == 2 * games.height
    later = minutes.join(games.select("game_id", "game_date"), on="game_id").filter(
        pl.col("game_date") > games["game_date"].min()
    )
    assert later["pp_minutes"].is_finite().all()
    league = history.filter(pl.col("season") == pf.SEASONS[1])["min_pp"].mean()
    assert later["pp_minutes"].mean() == pytest.approx(league, rel=0.2)


def fixture_lake(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from nhl_edge.lake.tables import Lake

    frames = pf.league()

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
    result = CliRunner().invoke(app, ["power-plays", "--seasons", "20112012", "--out", "out"])
    assert result.exit_code == 0, result.output
    (path,) = (tmp_path / "out").glob("power-plays-*.md")
    assert "# Expected power plays" in path.read_text()
    rates = pl.read_parquet(tmp_path / "data" / "lake" / "penalty_rates" / "**" / "*.parquet")
    expected = pl.read_parquet(
        tmp_path / "data" / "lake" / "expected_power_plays" / "**" / "*.parquet"
    )
    assert expected.height == 2 * 60
    # The fixture's heavy taker and drawer end the season on top.
    last = rates.filter(pl.col("game_date") == rates["game_date"].max())
    top = {
        c: last.filter(pl.col("component") == c).sort("mean")["player_id"][-1]
        for c in ("pen_taken", "pen_drawn")
    }
    assert top == {"pen_taken": pf.TAKER, "pen_drawn": pf.DRAWER}


def test_the_command_refuses_a_season_before_2011_12(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app

    fixture_lake(monkeypatch)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["power-plays", "--seasons", "20102011"])
    assert result.exit_code == 2


def test_games_without_strength_time_or_xg_and_dates_without_penalties_are_refused() -> None:
    # #130: a game or date missing from these would rate on part of the data, silently. A game
    # with no penalty is fine when its date has some.
    days = [date(2011, 10, 6), date(2011, 10, 6), date(2011, 10, 7), date(2010, 10, 7)]
    games = pl.DataFrame(
        {
            "game_id": [2011020001, 2011020002, 2011020003, 2010020001],
            "season": [20112012, 20112012, 20112012, 20102011],
            "game_date": days,
        },
        schema_overrides={"season": pl.Int32},
    )
    penalties = pl.DataFrame({"game_id": [2011020001], "game_date": [date(2011, 10, 6)]})
    strength_time = pl.DataFrame({"game_id": [2011020001, 2011020002, 2011020003]})
    shot_xg = pl.DataFrame({"game_id": [2011020001, 2011020003]})
    assert pr.input_problems(games, penalties, strength_time, shot_xg, 20112012) == [
        "20112012: 1 dates of games without penalties, e.g. 2011-10-07",
        "20112012: 1 games without xG, e.g. 2011020002",
    ]
    complete = pl.DataFrame({"game_id": [2011020001, 2011020002, 2011020003]})
    every_day = penalties.vstack(pl.DataFrame({"game_id": [2011020003], "game_date": days[2:3]}))
    assert pr.input_problems(games, every_day, complete, complete, 20112012) == []
    # Seasons after the last one read aren't checked.
    assert pr.input_problems(games, every_day, complete, complete, 20102011) == []


def test_the_command_refuses_a_lake_missing_a_game_s_strength_time(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app

    frames = fixture_lake(monkeypatch)
    first = frames["games"].filter(pl.col("season") == 20112012)["game_id"].min()
    frames["strength_time"] = frames["strength_time"].filter(pl.col("game_id") != first)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["power-plays", "--seasons", "20112012", "--out", "out"])
    assert result.exit_code == 1
    assert f"games without strength time, e.g. {first}" in result.output
    assert not (tmp_path / "data" / "lake" / "penalty_rates").exists()
