"""scripts/debut_nhle.py (#125, ADR 0050): its base fit is rapm.rate's, a1 gives a debutant his
NHLe prior once his lines are public, a2's coding of his rows reaches only later seasons' fits,
and its per-hour figures resample both sums together."""

from datetime import date

import numpy as np
import polars as pl
import pytest
import rapm_fixtures as fx
from debut_fixtures import DEBUT, LEAGUE, LONG, MIDSEASON, THREE, dn, fit, late, lines

from nhl_edge.backtest import metrics
from nhl_edge.ratings import priors, rapm

BASE, _, BASE_RUN = fit(dn.BASE)
PRIOR, _, PRIOR_RUN = fit(dn.PRIOR)


def test_the_base_fit_is_rapm_rate_s() -> None:
    wanted = rapm.targets(LEAGUE["lineups"], LEAGUE["games"], list(fx.SEASONS))
    ratings, _, _ = rapm.rate(
        fx.seasons_of(LEAGUE["stints"]),
        LEAGUE["games"],
        LEAGUE["roles"],
        LEAGUE["venues"],
        wanted,
        rapm.TUNED,
        "rapm-20261029-abc1234",
        players=fx.players(),
        league_seasons=lines(MIDSEASON),
        kinds=(rapm.EV,),
        spread=False,
    )
    ratings = ratings.sort("game_id", "player_id", "component")
    assert ratings.height == BASE.height
    for column in ("mean", "prior"):
        np.testing.assert_allclose(BASE[column].to_numpy(), ratings[column].to_numpy(), atol=1e-12)


def test_a1_gives_the_debutant_his_nhle_prior_once_his_lines_are_public() -> None:
    base, prior = late(BASE), late(PRIOR)
    after = (prior["as_of_utc"] > MIDSEASON).to_numpy()
    assert after.any() and not after.all()
    effects = PRIOR_RUN.effects[DEBUT]
    # His AHL season: 105 points in 70 games, at the fixture's AHL factor.
    nhle = 105 / 70 * fx.AHL_FACTOR
    expected = (
        base["prior"].to_numpy()
        + effects["has_nhle"]
        + effects["nhle"] * (nhle - priors.REFERENCE_PPG)
    )
    np.testing.assert_allclose(prior["prior"].to_numpy()[after], expected[after], atol=1e-12)
    np.testing.assert_allclose(
        prior["prior"].to_numpy()[~after], base["prior"].to_numpy()[~after], atol=1e-12
    )
    assert effects["has_nhle"] != 0


def test_a1_leaves_every_other_player_s_prior_alone() -> None:
    others = pl.col("player_id") != fx.LATE_PLAYER
    np.testing.assert_allclose(
        PRIOR.filter(others)["prior"].to_numpy(), BASE.filter(others)["prior"].to_numpy()
    )


def test_the_run_records_the_debutant_s_priors_and_public_time() -> None:
    (row,) = PRIOR_RUN.late[1].filter(pl.col("player_id") == fx.LATE_PLAYER).iter_rows(named=True)
    assert row["public_utc"] == MIDSEASON
    first = late(BASE)["prior"][0]
    assert row["prior_start"] == pytest.approx(first)
    assert row["prior_late"] == pytest.approx(late(PRIOR)["prior"][-1])


def test_a2_changes_nothing_in_the_debut_season_and_the_next_season_s_effects() -> None:
    prior, _, prior_run = fit(dn.PRIOR, league=LONG, seasons=THREE)
    rows, _, rows_run = fit(dn.ROWS, league=LONG, seasons=THREE)
    debut = pl.col("season") <= DEBUT
    np.testing.assert_allclose(
        rows.filter(debut)["mean"].to_numpy(), prior.filter(debut)["mean"].to_numpy(), atol=1e-12
    )
    assert rows_run.effects[DEBUT] == prior_run.effects[DEBUT]
    after = THREE[2]
    assert rows_run.effects[after]["has_nhle"] != pytest.approx(
        prior_run.effects[after]["has_nhle"]
    )


def test_the_ratio_resamples_both_sums_together() -> None:
    # Two weeks: one with a large error over many hours, one with a small error over few.
    frame = pl.DataFrame(
        {
            "season": [20122013] * 4,
            "game_date": [
                date(2012, 10, 8),
                date(2012, 10, 9),
                date(2012, 10, 15),
                date(2012, 10, 16),
            ],
            "d": [4.0, 4.0, 0.5, 0.5],
            "hours": [2.0, 2.0, 1.0, 1.0],
        }
    )
    interval = dn.ratio(frame, "d", "hours")
    assert interval.value == pytest.approx(9 / 6)
    # Each draw is one of its weeks' own ratios or the pooled one: never outside [0.5, 2].
    assert 0.5 - 1e-12 <= interval.low <= interval.value <= interval.high <= 2 + 1e-12
    fixed = metrics.bootstrap(frame, "d")
    # Dividing by a fixed mean of hours would reach 4 / 1.5.
    assert fixed.high / 1.5 > interval.high
