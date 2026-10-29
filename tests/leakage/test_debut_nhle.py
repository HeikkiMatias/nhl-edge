"""Point-in-time rules for scripts/debut_nhle.py (#125, ADR 0050, hard rule 1): a1 reads a
debutant's lines only once they are public strictly before an as-of time, never moves the
season's trait effects or any earlier rating, and each game's own stints never move its own
out-of-sample prediction."""

from datetime import datetime, timedelta

import numpy as np
import polars as pl
import rapm_fixtures as fx
from debut_fixtures import DEBUT, LEAGUE, MIDSEASON, as_ofs, dn, fit, late

BASE, BASE_STINTS, BASE_RUN = fit(dn.BASE)


def test_a_line_public_exactly_at_an_as_of_time_is_not_read() -> None:
    at = as_ofs()[10]
    ratings, _, _ = fit(dn.PRIOR, public=at)
    base, prior = late(BASE), late(ratings)
    same = (prior["as_of_utc"] <= at).to_numpy()
    np.testing.assert_allclose(
        prior["prior"].to_numpy()[same], base["prior"].to_numpy()[same], atol=1e-12
    )
    assert (prior["prior"].to_numpy()[~same] != base["prior"].to_numpy()[~same]).all()


def test_a1_never_moves_the_effects_or_any_rating_before_the_lines_are_public() -> None:
    ratings, _, run = fit(dn.PRIOR)
    assert run.effects == BASE_RUN.effects
    before = pl.col("as_of_utc") <= MIDSEASON
    for column in ("mean", "prior"):
        np.testing.assert_allclose(
            ratings.filter(before)[column].to_numpy(),
            BASE.filter(before)[column].to_numpy(),
            atol=1e-12,
        )


def test_lines_public_after_the_season_change_nothing_in_it() -> None:
    end = LEAGUE["games"].filter(pl.col("season") == DEBUT)["start_utc"].max()
    assert isinstance(end, datetime)
    ratings, _, run = fit(dn.PRIOR, public=end + timedelta(days=30))
    assert run.effects == BASE_RUN.effects
    np.testing.assert_allclose(ratings["mean"].to_numpy(), BASE["mean"].to_numpy(), atol=1e-12)


def test_a_game_s_own_stints_never_move_its_own_prediction() -> None:
    game = int(
        BASE_STINTS.filter(pl.col("season") == DEBUT)
        .sort("game_date")["game_id"]
        .unique(maintain_order=True)[5]
    )
    doubled = LEAGUE["stints"].with_columns(
        pl.when(pl.col("game_id") == game).then(pl.col(c) * 2).otherwise(pl.col(c)).alias(c)
        for c in ("home_xg", "away_xg")
    )
    _, stints, _ = fit(dn.BASE, stints=doubled)
    own = pl.col("game_id") == game
    np.testing.assert_allclose(
        stints.filter(own)["yhat"].to_numpy(), BASE_STINTS.filter(own)["yhat"].to_numpy()
    )
    assert (stints.filter(own)["y"] != BASE_STINTS.filter(own)["y"]).any()
    later = pl.col("game_date") > BASE_STINTS.filter(own)["game_date"].max()
    assert not np.allclose(
        stints.filter(later)["yhat"].to_numpy(), BASE_STINTS.filter(later)["yhat"].to_numpy()
    )


def test_a_debutant_on_the_ice_is_flagged_only_once_his_lines_are_public() -> None:
    _, stints, _ = fit(dn.PRIOR)
    games = LEAGUE["games"].filter(pl.col("season") == DEBUT)
    flagged = stints.filter("debutant_on").join(games.select("game_id", "start_utc"), on="game_id")
    assert flagged.height
    assert (flagged["start_utc"] > MIDSEASON).all()
    assert not BASE_STINTS.filter(pl.col("season") == fx.SEASONS[0])["debutant_on"].any()
