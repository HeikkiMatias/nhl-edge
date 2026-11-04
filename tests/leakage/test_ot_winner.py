"""Point in time for scripts/ot_winner.py (#379 step 1, hard rule 1): each scored season's fits
read only earlier seasons' ties, and the seasons read are open ones only."""

import polars as pl
from ot_winner_fixtures import league, load

from nhl_edge.backtest.seasons import SEASON_ROLES, SeasonRole

ow = load()


def test_a_later_season_never_changes_an_earlier_seasons_predictions() -> None:
    tied, inputs, games, rates = league(0.5)
    base, _, fits = ow.predictions(tied, inputs, games, rates)
    flipped = tied.with_columns(
        home_won=pl.when(pl.col("season") >= 20182019)
        .then(1 - pl.col("home_won"))
        .otherwise(pl.col("home_won"))
    )
    after, _, fits_after = ow.predictions(flipped, inputs, games, rates)
    early = pl.col("season") <= 20172018
    cols = ["game_id", "q0", "q1", "q2", "q3"]
    assert (
        after.filter(early)
        .select(cols)
        .sort("game_id")
        .equals(base.filter(early).select(cols).sort("game_id"))
    )
    assert fits["20172018"] == fits_after["20172018"]


def test_only_open_seasons_are_read_and_q3_only_on_training_seasons() -> None:
    open_roles = {SeasonRole.TRAINING, SeasonRole.TRAINING_FLAGGED, SeasonRole.DEVELOPMENT}
    seasons = [s for s in SEASON_ROLES if ow.FIRST <= s <= ow.LAST]
    assert all(SEASON_ROLES[s] in open_roles for s in seasons)
    assert ow.LAST == 20212022 and SEASON_ROLES[ow.LAST + 10001] is SeasonRole.MARKET_VALIDATION
    assert all(SEASON_ROLES[s] is SeasonRole.TRAINING for s in ow.Q3_SCORED)
    assert all(SEASON_ROLES[s] is not SeasonRole.TRAINING_FLAGGED for s in ow.SCORED)


def test_the_draw_rate_for_a_shift_reads_only_earlier_seasons() -> None:
    tied, inputs, games, rates = league(0.5)
    later = dict(rates) | {20212022: 0.9}
    _, _, fits = ow.predictions(tied, inputs, games, rates)
    _, _, fits_later = ow.predictions(tied, inputs, games, later)
    assert fits["20212022"]["draw_rate"] == fits_later["20212022"]["draw_rate"]
