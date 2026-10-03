"""Point-in-time rules for finishing and goalie conversion (#105, ADR 0022, hard rule 1). A
game's φ, xG shares and goal multipliers read only shots, xG, stints and boxscores public before
its as-of time, and the goalie effects of its own game, which are themselves point in time
(#75): its own night's and later games never move them, a game public exactly at the as-of time
counts as not yet public, a season's own games never move its pulls, and later games' goalie
effects never move a game's gamma."""

from datetime import date
from typing import Any

import finishing_fixtures as ff
import numpy as np
import polars as pl

from nhl_edge.features import team_strength as ts
from nhl_edge.lake.schemas import Finishing, GoalMultipliers
from nhl_edge.lineup import minutes as mins
from nhl_edge.ratings import finishing as fn

FRAMES = ff.league()
GAMES = FRAMES["games"]
SEASON = ff.SEASONS[1]
DATES = GAMES.filter(pl.col("season") == SEASON)["game_date"].unique().sort()
OPENING, NIGHT = DATES[0], DATES[10]
VERSION = "finishing-20261003-abc1234"


def run(**changes: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame, fn.SeasonPulls]:
    """Finishing and goal multipliers for the opening night and NIGHT, from the fixture with
    some tables replaced."""
    f: dict[str, Any] = {**FRAMES, **changes}
    minutes = mins.player_minutes(f["stints"], f["actual_lineups"])
    rows = fn.shooter_games(minutes, f["shots"], f["shot_xg"], GAMES)
    pulls = {SEASON: fn.season_pulls(rows, SEASON, GAMES)}
    nights = GAMES.filter(pl.col("game_date").is_in([OPENING, NIGHT]))
    as_of = ts.as_of(pl.col("game_date"), pl.col("start_utc"))
    candidates = (
        FRAMES["lineups"]
        .filter(pl.col("game_id").is_in(nights["game_id"].implode()))
        .join(GAMES.select("game_id", as_of_utc=as_of), on="game_id")
    )
    times = nights.select("season", as_of_utc=as_of)
    rated = fn.rates(
        candidates.select(
            "game_id", "season", "game_date", "team", "player_id", "role", "as_of_utc"
        ),
        rows,
        pulls,
    )
    shared, multipliers = fn.multipliers(
        rated,
        candidates,
        FRAMES["lineup_replacements"],
        fn.league_rates(rows, times),
        f["goalie_effects"].filter(pl.col("game_id").is_in(nights["game_id"].implode())),
        fn.shot_figures(f["shots"], f["shot_xg"], times),
        nights,
    )
    return (
        fn.stamp(shared, Finishing, pulls, VERSION).sort("game_id", "player_id"),
        fn.stamp(multipliers, GoalMultipliers, pulls, VERSION).sort("game_id", "team", "goalie_id"),
        pulls[SEASON],
    )


BASE, BASE_MULTIPLIERS, BASE_PULLS = run()
FINISHING_OUTPUTS = ("phi", "phi_sd", "goals", "expected_goals", "xg_rate", "hours", "share")
MULTIPLIER_OUTPUTS = ("phi", "gamma", "multiplier", "xg_per_shot", "league_finishing")


def on(frame: pl.DataFrame, night: date) -> pl.DataFrame:
    return frame.filter(pl.col("game_date") == night)


def same(a: pl.DataFrame, b: pl.DataFrame, columns: tuple[str, ...]) -> None:
    assert a.height == b.height
    for column in columns:
        np.testing.assert_allclose(
            a[column].fill_null(-1.0).to_numpy(),
            b[column].fill_null(-1.0).to_numpy(),
            rtol=1e-9,
            atol=1e-12,
        )


def changed_from(night: date) -> dict[str, pl.DataFrame]:
    """Every per-game table from the night on, changed: every shot a goal by the sniper with
    twice its xG, stints' skaters dropped, roles swapped, and the goalie effects reversed."""
    later = pl.col("game_date") >= night
    dated = GAMES.select("game_id", "game_date")
    swapped = (
        pl.when(pl.col("role") == "D")
        .then(pl.lit("F"))
        .when(pl.col("role") == "F")
        .then(pl.lit("D"))
    ).otherwise(pl.col("role"))
    return {
        "shots": FRAMES["shots"]
        .join(dated, on="game_id")
        .with_columns(
            is_goal=pl.when(later).then(True).otherwise("is_goal"),
            shooter_id=pl.when(later).then(pl.lit(ff.SNIPER, pl.Int64)).otherwise("shooter_id"),
        )
        .drop("game_date"),
        "shot_xg": FRAMES["shot_xg"]
        .join(dated, on="game_id")
        .with_columns(xg=pl.when(later).then(pl.col("xg") * 2).otherwise("xg"))
        .drop("game_date"),
        "stints": FRAMES["stints"].filter(~later),
        "actual_lineups": FRAMES["actual_lineups"].with_columns(
            role=pl.when(later).then(swapped).otherwise(pl.col("role"))
        ),
        "goalie_effects": FRAMES["goalie_effects"].with_columns(
            effect=pl.when(pl.col("game_date") > night).then(-pl.col("effect")).otherwise("effect")
        ),
    }


def test_tonights_and_later_games_never_move_tonights_finishing_or_multipliers() -> None:
    finishing, multipliers, pulls = run(**changed_from(NIGHT))
    same(on(finishing, NIGHT), on(BASE, NIGHT), FINISHING_OUTPUTS)
    same(on(multipliers, NIGHT), on(BASE_MULTIPLIERS, NIGHT), MULTIPLIER_OUTPUTS)
    assert pulls.finishing == BASE_PULLS.finishing and pulls.rate == BASE_PULLS.rate


def test_the_season_s_own_games_never_move_its_opening_night_or_its_pulls() -> None:
    finishing, multipliers, pulls = run(**changed_from(OPENING))
    same(on(finishing, OPENING), on(BASE, OPENING), FINISHING_OUTPUTS)
    same(on(multipliers, OPENING), on(BASE_MULTIPLIERS, OPENING), MULTIPLIER_OUTPUTS)
    assert pulls.finishing == BASE_PULLS.finishing and pulls.rate == BASE_PULLS.rate


def test_earlier_games_do_move_them() -> None:
    before = pl.col("game_date") < NIGHT
    shots = (
        FRAMES["shots"]
        .join(GAMES.select("game_id", "game_date"), on="game_id")
        .with_columns(
            is_goal=pl.when(before & (pl.col("shooter_id") == ff.SNIPER))
            .then(False)
            .otherwise("is_goal")
        )
        .drop("game_date")
    )
    finishing, multipliers, pulls = run(shots=shots)
    assert not np.allclose(on(finishing, NIGHT)["phi"], on(BASE, NIGHT)["phi"])
    assert not np.allclose(
        on(multipliers, NIGHT)["league_finishing"], on(BASE_MULTIPLIERS, NIGHT)["league_finishing"]
    )
    assert pulls.finishing != BASE_PULLS.finishing


def test_a_game_public_at_the_as_of_time_is_not_read() -> None:
    # The night before's shots and xG, stamped public exactly at tonight's as-of time, count as
    # not yet public: the same as leaving that night out.
    before = GAMES.filter(pl.col("game_date") < NIGHT)["game_date"].max()
    as_of = BASE.filter(pl.col("game_date") == NIGHT)["as_of_utc"].min()
    late = pl.col("game_id").is_in(GAMES.filter(pl.col("game_date") == before)["game_id"].implode())
    stamped = {
        t: FRAMES[t].with_columns(
            observed_utc=pl.when(late).then(pl.lit(as_of)).otherwise(pl.col("observed_utc"))
        )
        for t in ("shots", "shot_xg")
    }
    left_out = {t: FRAMES[t].filter(~late) for t in ("shots", "shot_xg")}
    a, a_multipliers, _ = run(**stamped)
    b, b_multipliers, _ = run(**left_out)
    same(on(a, NIGHT), on(b, NIGHT), FINISHING_OUTPUTS)
    same(on(a_multipliers, NIGHT), on(b_multipliers, NIGHT), MULTIPLIER_OUTPUTS)


def test_xg_alone_public_at_the_as_of_time_is_not_read() -> None:
    # The night before's xG alone stamped late leaves that night unread, the same as its shots
    # stamped so.
    before = GAMES.filter(pl.col("game_date") < NIGHT)["game_date"].max()
    as_of = BASE.filter(pl.col("game_date") == NIGHT)["as_of_utc"].min()
    late = pl.col("game_id").is_in(GAMES.filter(pl.col("game_date") == before)["game_id"].implode())

    def stamped(table: str) -> pl.DataFrame:
        return FRAMES[table].with_columns(
            observed_utc=pl.when(late).then(pl.lit(as_of)).otherwise(pl.col("observed_utc"))
        )

    a, a_multipliers, _ = run(shot_xg=stamped("shot_xg"))
    b, b_multipliers, _ = run(shots=stamped("shots"))
    same(on(a, NIGHT), on(b, NIGHT), FINISHING_OUTPUTS)
    same(on(a_multipliers, NIGHT), on(b_multipliers, NIGHT), MULTIPLIER_OUTPUTS)
    assert not np.allclose(on(a, NIGHT)["phi"], on(BASE, NIGHT)["phi"])


def test_stints_alone_public_at_the_as_of_time_are_not_read() -> None:
    # The night before's stints alone stamped late leave that night's skater-games unread, the
    # same as its shots and xG stamped so: its shots no longer count toward anyone's φ.
    before = GAMES.filter(pl.col("game_date") < NIGHT)["game_date"].max()
    as_of = BASE.filter(pl.col("game_date") == NIGHT)["as_of_utc"].min()
    late = pl.col("game_id").is_in(GAMES.filter(pl.col("game_date") == before)["game_id"].implode())

    def stamped(table: str) -> pl.DataFrame:
        return FRAMES[table].with_columns(
            observed_utc=pl.when(late).then(pl.lit(as_of)).otherwise(pl.col("observed_utc"))
        )

    a, _, _ = run(stints=stamped("stints"))
    b, _, _ = run(shots=stamped("shots"), shot_xg=stamped("shot_xg"))
    same(
        on(a, NIGHT), on(b, NIGHT), ("phi", "phi_sd", "goals", "expected_goals", "xg_rate", "hours")
    )
    assert not np.allclose(on(a, NIGHT)["phi"], on(BASE, NIGHT)["phi"])


def test_every_row_was_read_before_its_as_of_time() -> None:
    for frame in (BASE, BASE_MULTIPLIERS):
        known = frame.filter(pl.col("known_utc").is_not_null())
        assert (known["known_utc"] < known["as_of_utc"]).all()
        assert (frame["observed_utc"] >= frame["as_of_utc"]).all()
    assert BASE_PULLS.cutoff is not None
    first = (
        GAMES.filter(pl.col("season") == SEASON)
        .select(ts.as_of(pl.col("game_date"), pl.col("start_utc")).min())
        .item()
    )
    assert BASE_PULLS.cutoff < first
