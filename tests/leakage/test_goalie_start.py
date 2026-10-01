"""Point-in-time rules for the goalie-start model (#76, ADR 0012, hard rules 1 and 9). A
team-game's start probabilities read only boxscores public before its as-of time: 10:00 US
Eastern on the game date, or an hour before the start if that is earlier. Its own boxscore and
later ones never move them, and each season's model is fitted only on earlier seasons' starters
public before the season's first as-of time."""

from datetime import timedelta

import numpy as np
import polars as pl
from goalie_fixtures import league
from polars.testing import assert_frame_equal

from nhl_edge.backtest.market import PREDICTION_LAG
from nhl_edge.features.team_strength import as_of
from nhl_edge.ingest.sbr import open_assumed_utc
from nhl_edge.lake.tables import known_at
from nhl_edge.lineup import goalie_start as gs

LEAGUE = league()
GAMES, LINEUPS = LEAGUE["games"], LEAGUE["lineups"]
VERSION = "goalie-start-20261001-abc1234"
SEASON = 20122013
# A night in the middle of 2012-13, with its earlier nights behind it.
NIGHT = GAMES.filter(pl.col("season") == SEASON)["game_date"].unique().sort()[20]
TONIGHT = GAMES.filter(pl.col("game_date") == NIGHT)


def inputs(lineups: pl.DataFrame) -> pl.DataFrame:
    """Tonight's candidates and their inputs, without the labels."""
    return gs.candidates(TONIGHT, lineups, {}).drop("started", "starter_utc")


BEFORE = inputs(LINEUPS)


def same(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    try:
        assert_frame_equal(left, right)
    except AssertionError:
        return False
    return True


def swapped(lineups: pl.DataFrame, rows: pl.Expr) -> pl.DataFrame:
    """The lineups with the chosen goalie rows' starter flags flipped: the other goalie started."""
    goalie = pl.col("role") == "G"
    return lineups.with_columns(
        starting_goalie=pl.when(rows & goalie)
        .then(~pl.col("starting_goalie"))
        .otherwise(pl.col("starting_goalie"))
    )


def test_tonights_and_later_boxscores_never_move_tonights_inputs() -> None:
    changed = swapped(LINEUPS, pl.col("game_date") >= NIGHT)
    assert same(inputs(changed), BEFORE)
    # Nor does dropping them: tonight's own boxscore is never read.
    assert same(inputs(LINEUPS.filter(pl.col("game_date") < NIGHT)), BEFORE)


def test_earlier_boxscores_do_move_them() -> None:
    # The guard above is not vacuous.
    changed = swapped(LINEUPS, pl.col("game_date") < NIGHT)
    assert not same(inputs(changed), BEFORE)


def test_a_boxscore_public_at_the_as_of_time_is_not_read() -> None:
    moment = TONIGHT.select(as_of(pl.col("game_date"), pl.col("start_utc"))).item(0, 0)
    last_night = GAMES.filter(pl.col("game_date") < NIGHT)["game_date"].max()
    late = pl.col("game_date") == last_night

    def public_at(moment: object) -> pl.DataFrame:
        observed = pl.when(late).then(pl.lit(moment)).otherwise(pl.col("observed_utc"))
        return LINEUPS.with_columns(observed_utc=observed)

    assert same(inputs(public_at(moment)), inputs(LINEUPS.filter(~late)))
    assert same(inputs(public_at(moment - timedelta(microseconds=1))), BEFORE)
    assert not same(BEFORE, inputs(LINEUPS.filter(~late)))


def coefficients(lineups: pl.DataFrame) -> np.ndarray:
    rows = gs.candidates(GAMES, lineups, {})
    return np.asarray(gs.fit(rows, GAMES, SEASON, VERSION).coefficients)


def test_the_seasons_own_starters_are_never_fitted_on() -> None:
    changed = swapped(LINEUPS, pl.col("season") >= SEASON)
    np.testing.assert_allclose(coefficients(changed), coefficients(LINEUPS), rtol=0, atol=1e-12)


def test_an_earlier_starter_published_after_the_cutoff_is_not_fitted_on() -> None:
    # 2011-12's last night, as if its boxscores came out only after 2012-13's first as-of time
    # and named the other starters: the fit is the one without those boxscores at all.
    cutoff = gs.season_cutoff(GAMES, SEASON)
    last = GAMES.filter(pl.col("season") == SEASON - 10001)["game_date"].max()
    late = pl.col("game_date") == last
    published_late = swapped(LINEUPS, late).with_columns(
        observed_utc=pl.when(late)
        .then(pl.lit(cutoff + timedelta(hours=1)))
        .otherwise(pl.col("observed_utc"))
    )
    left_out = LINEUPS.filter(~late)
    np.testing.assert_allclose(
        coefficients(published_late), coefficients(left_out), rtol=0, atol=1e-12
    )
    assert not np.allclose(coefficients(swapped(LINEUPS, late)), coefficients(LINEUPS))


def test_every_probability_is_known_before_e1_and_e2_and_after_its_model() -> None:
    table, _, models = gs.score(LINEUPS, GAMES, [20112012, SEASON], VERSION, {})
    starts = GAMES.select("game_id", "start_utc")
    for row in table.join(starts, on="game_id").iter_rows(named=True):
        public = row["start_utc"] - timedelta(days=30)
        e2 = open_assumed_utc(row["game_date"], row["start_utc"], public) + PREDICTION_LAG
        assert row["train_cutoff"] < row["observed_utc"] < min(e2, row["start_utc"])
    for model in models:
        first = GAMES.filter(pl.col("season") == model.season)["start_utc"].min()
        assert model.train_cutoff < gs.season_cutoff(GAMES, model.season) <= first  # type: ignore[operator]
        rows = table.filter(pl.col("season") == model.season)
        # Nothing of the season is known before the model existed.
        assert known_at(rows, model.train_cutoff + timedelta(microseconds=1)).is_empty()
