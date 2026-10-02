"""Point-in-time rules for projected ice time (#100, ADR 0018, hard rules 1 and 9). A candidate's
minutes read only games whose stints were public before his game's as-of time; the game's own
stints and later ones never move them. Each season's averages, pulls, totals and newcomer minutes
come from the season before, all public before the season's first as-of time."""

from datetime import timedelta

import numpy as np
import polars as pl
import pytest
from lineup_fixtures import league, minutes_frame

from nhl_edge.audit import projection as report
from nhl_edge.lake.schemas import LineupReplacements, dtypes
from nhl_edge.lineup import goalie_start as gs
from nhl_edge.lineup import minutes as mins
from nhl_edge.lineup import projection as pr

LEAGUE = league()
GAMES, LINEUPS = LEAGUE["games"], LEAGUE["lineups"]
MINUTES = minutes_frame(LINEUPS)
ROWS = pr.candidates(GAMES, LINEUPS, {})
VERSION = "lineup-20261002-abc1234"
SEASON = 20122013
NIGHT = GAMES.filter(pl.col("season") == SEASON)["game_date"].unique().sort()[20]
TONIGHT = GAMES.filter(pl.col("game_date") == NIGHT)["game_id"]
OUTPUTS = ("min_5v5", "min_pp", "min_pk", "exp_5v5", "exp_pp", "exp_pk", "last_5v5")
_, SCORED, _ = pr.score(LINEUPS, GAMES, [SEASON], VERSION, {}, ROWS)
CONSTANTS = {SEASON: mins.season_constants(MINUTES, ROWS, SEASON, GAMES)}
REPLACEMENT_COLUMNS = [
    "game_id",
    "season",
    "game_date",
    "team",
    "role",
    "count",
    "exp_5v5",
    "exp_pp",
    "exp_pk",
    "train_cutoff",
    "artifact_version",
    "observed_utc",
]


def tonight(minutes: pl.DataFrame) -> pl.DataFrame:
    rows, _ = mins.project(SCORED, minutes, CONSTANTS, GAMES, {})
    return rows.filter(pl.col("game_id").is_in(TONIGHT.implode())).sort(
        "game_id", "team", "player_id"
    )


def doubled(where: pl.Expr) -> pl.DataFrame:
    """MINUTES with the games where holds playing twice their minutes."""
    return MINUTES.with_columns(
        pl.when(where).then(pl.col(state) * 2).otherwise(pl.col(state)) for state in mins.STATES
    )


def test_tonights_and_later_minutes_never_move_tonights_projection() -> None:
    base = tonight(MINUTES)
    moved = tonight(doubled(pl.col("game_date") >= NIGHT))
    for column in OUTPUTS:
        np.testing.assert_allclose(moved[column].to_numpy(), base[column].to_numpy(), atol=1e-12)


def test_earlier_minutes_do_move_it() -> None:
    moved = tonight(doubled(pl.col("game_date") < NIGHT))
    assert not np.allclose(moved["min_5v5"].to_numpy(), tonight(MINUTES)["min_5v5"].to_numpy())


def test_a_game_public_at_the_as_of_time_is_not_read() -> None:
    # The night before's stints, stamped public exactly at tonight's as-of time, count as not yet
    # public: the same as leaving them out.
    before = GAMES.filter(pl.col("game_date") < NIGHT)["game_date"].max()
    as_of = SCORED.filter(pl.col("game_id").is_in(TONIGHT.implode()))["as_of_utc"].min()
    late = pl.col("game_date") == before
    stamped = MINUTES.with_columns(
        observed_utc=pl.when(late).then(pl.lit(as_of)).otherwise(pl.col("observed_utc"))
    )
    left_out = MINUTES.filter(~late)
    a, b = tonight(stamped), tonight(left_out)
    for column in OUTPUTS:
        np.testing.assert_allclose(
            a[column].fill_null(-1.0).to_numpy(), b[column].fill_null(-1.0).to_numpy(), atol=1e-12
        )


def test_a_season_s_constants_read_only_the_season_before() -> None:
    base = mins.season_constants(MINUTES, ROWS, SEASON, GAMES)
    moved = mins.season_constants(doubled(pl.col("season") >= SEASON), ROWS, SEASON, GAMES)
    # Equal up to the order in which sums are taken.
    for name in ("mean", "pull", "total", "newcomer"):
        a, b = getattr(base, name), getattr(moved, name)
        assert a.keys() == b.keys()
        np.testing.assert_allclose([b[k] for k in a], list(a.values()), rtol=1e-12)
    assert moved.cutoff == base.cutoff
    assert base.cutoff < gs.season_cutoff(GAMES, SEASON)
    changed = mins.season_constants(
        doubled(pl.col("season") == SEASON - 10_001), ROWS, SEASON, GAMES
    )
    assert changed.total != base.total


def test_every_replacement_row_is_known_after_its_figures() -> None:
    skaters, scored, _ = pr.score(LINEUPS, GAMES, [SEASON], VERSION, {}, ROWS)
    rows, replacements = mins.project(scored, MINUTES, CONSTANTS, GAMES, {})
    table = mins.replacement_table(replacements, mins.with_minutes(skaters, rows, CONSTANTS))
    assert (table["train_cutoff"] < table["observed_utc"]).all()
    assert (table["train_cutoff"] >= CONSTANTS[SEASON].cutoff).all()
    assert (table["observed_utc"] - table["train_cutoff"] > timedelta(0)).all()


def test_the_replacement_column_set_is_locked() -> None:
    assert list(dtypes(LineupReplacements)) == REPLACEMENT_COLUMNS


LAST_GAME_BEFORE = MINUTES.filter(pl.col("season") == SEASON - 10_001)["game_id"].max()


def test_constants_refuse_a_game_of_the_season_before_public_too_late() -> None:
    first = gs.season_cutoff(GAMES, SEASON)
    late = MINUTES.with_columns(
        observed_utc=pl.when(pl.col("game_id") == LAST_GAME_BEFORE)
        .then(pl.lit(first))
        .otherwise(pl.col("observed_utc"))
    )
    with pytest.raises(ValueError, match="not before"):
        mins.season_constants(late, ROWS, SEASON, GAMES)


def test_the_report_hides_a_held_out_season_s_figures_and_those_taken_from_it() -> None:
    _, scored, _ = pr.score(LINEUPS, GAMES, [20112012, SEASON], VERSION, {}, ROWS)
    constants = {s: mins.season_constants(MINUTES, ROWS, s, GAMES) for s in (20112012, SEASON)}
    rows, _ = mins.project(scored, MINUTES, constants, GAMES, {})
    ice = report.ice_time_scores(rows, MINUTES)
    scores = report.team_game_scores(scored, LINEUPS)
    # Only 2012-13 is shown: 2011-12's ice time and 2012-13's figures from it stay hidden.
    text = report.markdown_report(
        scores, [], [SEASON], VERSION, ice, [constants[s] for s in (20112012, SEASON)]
    )
    ice_rows = text.split("## Ice time")[1].split("## Ice-time figures")[0]
    (held,) = [line for line in ice_rows.splitlines() if line.startswith("| 20112012 |")]
    assert "held out" in held and "[" not in held
    figures = text.split("## Ice-time figures from the season before")[1]
    (taken,) = [line for line in figures.splitlines() if line.startswith("| 20122013 | 20112012 |")]
    assert "held out" in taken and not any(ch.isdigit() for ch in taken.split("| 20112012 |")[1])
