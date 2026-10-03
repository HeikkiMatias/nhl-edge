"""Point-in-time rules for penalty rates and expected power plays (#104, ADR 0021, hard rule 1).
A game's rates and expected power plays read only penalties, stints, boxscores, strength time and
shots public before its as-of time: its own night's and later ones never move them, one public
exactly at the as-of time counts as not yet public, and a season's own games never move its
pulls."""

from datetime import date
from typing import Any

import numpy as np
import penalty_fixtures as pf
import polars as pl

from nhl_edge.features import team_strength as ts
from nhl_edge.lake.schemas import ExpectedPowerPlays, PenaltyRates
from nhl_edge.lineup import minutes as mins
from nhl_edge.ratings import penalty_rates as pr

FRAMES = pf.league()
GAMES = FRAMES["games"]
SEASON = pf.SEASONS[1]
DATES = GAMES.filter(pl.col("season") == SEASON)["game_date"].unique().sort()
OPENING, NIGHT = DATES[0], DATES[10]
VERSION = "power-plays-20261003-abc1234"


def run(**changes: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame, pr.SeasonPulls]:
    """Rates and expected power plays for the opening night and NIGHT, from the fixture with
    some tables replaced."""
    f: dict[str, Any] = {**FRAMES, **changes}
    minutes = mins.player_minutes(f["stints"], f["actual_lineups"])
    weighted = pr.unoffset(f["penalties"])
    rows = pr.player_games(minutes, weighted, GAMES)
    pulls = {SEASON: pr.season_pulls(rows, SEASON, GAMES)}
    nights = GAMES.filter(pl.col("game_date").is_in([OPENING, NIGHT]))
    as_of = ts.as_of(pl.col("game_date"), pl.col("start_utc"))
    candidates = (
        FRAMES["lineups"]
        .filter(pl.col("game_id").is_in(nights["game_id"].implode()))
        .join(GAMES.select("game_id", as_of_utc=as_of), on="game_id")
    )
    times = nights.select("season", as_of_utc=as_of)
    rated = pr.rates(
        candidates.select(
            "game_id", "season", "game_date", "team", "player_id", "role", "as_of_utc"
        ),
        rows,
        pulls,
    )
    expected = pr.expected(
        rated,
        candidates,
        FRAMES["lineup_replacements"],
        pr.role_rates(rows, times),
        pr.league_figures(
            pr.team_games(f["strength_time"], weighted, f["shots"], f["shot_xg"]), times
        ),
        nights,
    )
    return (
        pr.stamp(rated, PenaltyRates, pulls, VERSION).sort("game_id", "player_id", "component"),
        pr.stamp(expected, ExpectedPowerPlays, pulls, VERSION).sort("game_id", "team"),
        pulls[SEASON],
    )


BASE, BASE_EXPECTED, BASE_PULLS = run()
RATE_OUTPUTS = ("mean", "prior", "sd", "hours")
EXPECTED_OUTPUTS = (
    "taken_index",
    "drawn_index",
    "opportunities",
    "pp_minutes",
    "pk_minutes",
    "sh_xg",
    "league_opportunities",
    "pp_length",
    "sh_xg_per_pk_minute",
)


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
    """Every per-game table from the night on, changed: penalties all taken by the fixture's
    heavy taker's team at once, stints' skaters dropped, roles swapped, and twice the strength
    time and xG."""
    later = pl.col("game_date") >= night
    swapped = pl.when(pl.col("role") == "D").then(pl.lit("F")).otherwise(pl.lit("D"))
    return {
        "penalties": FRAMES["penalties"].with_columns(
            committed_by=pl.when(later).then(pl.lit(pf.TAKER, pl.Int64)).otherwise("committed_by"),
            duration_min=pl.when(later).then(pl.lit(5, pl.Int8)).otherwise("duration_min"),
        ),
        "stints": FRAMES["stints"].filter(~later),
        "actual_lineups": FRAMES["actual_lineups"].with_columns(
            role=pl.when(later).then(swapped).otherwise(pl.col("role"))
        ),
        "strength_time": FRAMES["strength_time"].with_columns(
            seconds=pl.when(later).then(pl.col("seconds") * 2).otherwise("seconds")
        ),
        "shot_xg": FRAMES["shot_xg"]
        .join(GAMES.select("game_id", "game_date"), on="game_id")
        .with_columns(xg=pl.when(later).then(pl.col("xg") * 2).otherwise("xg"))
        .drop("game_date"),
    }


def test_tonights_and_later_games_never_move_tonights_rates_or_power_plays() -> None:
    rates, expected, pulls = run(**changed_from(NIGHT))
    same(rates, BASE, RATE_OUTPUTS)
    same(expected, BASE_EXPECTED, EXPECTED_OUTPUTS)
    assert pulls.pull == BASE_PULLS.pull


def test_the_season_s_own_games_never_move_its_opening_night_or_its_pulls() -> None:
    rates, expected, pulls = run(**changed_from(OPENING))
    same(on(rates, OPENING), on(BASE, OPENING), RATE_OUTPUTS)
    same(on(expected, OPENING), on(BASE_EXPECTED, OPENING), EXPECTED_OUTPUTS)
    assert pulls.pull == BASE_PULLS.pull


def test_earlier_games_do_move_them() -> None:
    before = pl.col("game_date") < NIGHT
    penalties = FRAMES["penalties"].with_columns(
        committed_by=pl.when(before).then(pl.lit(pf.TAKER, pl.Int64)).otherwise("committed_by")
    )
    rates, expected, _ = run(penalties=penalties)
    assert not np.allclose(on(rates, NIGHT)["mean"], on(BASE, NIGHT)["mean"])
    assert not np.allclose(
        on(expected, NIGHT)["opportunities"], on(BASE_EXPECTED, NIGHT)["opportunities"]
    )


def test_a_game_public_at_the_as_of_time_is_not_read() -> None:
    # The night before's tables, stamped public exactly at tonight's as-of time, count as not yet
    # public: the same as leaving them out.
    before = GAMES.filter(pl.col("game_date") < NIGHT)["game_date"].max()
    as_of = BASE.filter(pl.col("game_date") == NIGHT)["as_of_utc"].min()
    late = pl.col("game_date") == before
    tables = ("penalties", "stints", "strength_time")
    stamped = {
        t: FRAMES[t].with_columns(
            observed_utc=pl.when(late).then(pl.lit(as_of)).otherwise(pl.col("observed_utc"))
        )
        for t in tables
    }
    left_out = {t: FRAMES[t].filter(~late) for t in tables}
    a, a_expected, _ = run(**stamped)
    b, b_expected, _ = run(**left_out)
    same(on(a, NIGHT), on(b, NIGHT), RATE_OUTPUTS)
    same(on(a_expected, NIGHT), on(b_expected, NIGHT), EXPECTED_OUTPUTS)


def test_every_row_was_read_before_its_as_of_time() -> None:
    assert (BASE["known_utc"] < BASE["as_of_utc"]).all()
    assert (BASE["observed_utc"] >= BASE["as_of_utc"]).all()
    assert (BASE_EXPECTED["observed_utc"] >= BASE_EXPECTED["as_of_utc"]).all()
    assert BASE_PULLS.source == pf.SEASONS[0]
