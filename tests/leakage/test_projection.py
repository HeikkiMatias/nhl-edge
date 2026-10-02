"""Point-in-time rules for the lineup model (#99, ADR 0017, hard rules 1 and 9). A team-game's
probabilities read only boxscores public before its as-of time: 10:00 US Eastern on the game
date, or an hour before the start if that is earlier. Its own boxscore and later ones never move
them, nor decide who dropped out for dressing elsewhere. Each season's model and its expected
newcomers are fitted only on earlier seasons' boxscores public before the season's first as-of
time."""

from datetime import timedelta

import numpy as np
import polars as pl
from lineup_fixtures import league
from polars.testing import assert_frame_equal

from nhl_edge.backtest.market import PREDICTION_LAG
from nhl_edge.features.team_strength import as_of
from nhl_edge.ingest.sbr import open_assumed_utc
from nhl_edge.lake.tables import known_at
from nhl_edge.lineup import goalie_start as gs
from nhl_edge.lineup import projection as pr

LEAGUE = league()
GAMES, LINEUPS = LEAGUE["games"], LEAGUE["lineups"]
VERSION = "lineup-20261002-abc1234"
SEASON = 20122013
# A night in the middle of 2012-13, with its earlier nights behind it.
NIGHT = GAMES.filter(pl.col("season") == SEASON)["game_date"].unique().sort()[20]
TONIGHT = GAMES.filter(pl.col("game_date") == NIGHT)
LABELS = ("dressed", "label_utc", "skaters_dressed")


def inputs(lineups: pl.DataFrame) -> pl.DataFrame:
    """Tonight's candidates and their inputs, without the labels."""
    return pr.candidates(TONIGHT, lineups, {}).drop(LABELS)


BEFORE = inputs(LINEUPS)


def same(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    try:
        assert_frame_equal(left, right)
    except AssertionError:
        return False
    return True


def altered(lineups: pl.DataFrame, rows: pl.Expr) -> pl.DataFrame:
    """The lineups with the chosen boxscores changed: a third of their skaters left out, and the
    rest leaving early."""
    skater = pl.col("role") != "G"
    chosen = rows & skater
    return lineups.filter(~(chosen & (pl.col("player_id") % 3 == 0))).with_columns(
        toi_s=pl.when(chosen).then(pl.lit(1, dtype=pl.Int32)).otherwise(pl.col("toi_s"))
    )


def test_tonights_and_later_boxscores_never_move_tonights_inputs() -> None:
    changed = altered(LINEUPS, pl.col("game_date") >= NIGHT)
    assert same(inputs(changed), BEFORE)
    # Nor does dropping them: tonight's own boxscore is never read.
    assert same(inputs(LINEUPS.filter(pl.col("game_date") < NIGHT)), BEFORE)


def test_earlier_boxscores_do_move_them() -> None:
    # The guard above is not vacuous.
    changed = altered(LINEUPS, pl.col("game_date") < NIGHT)
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


def test_dressing_elsewhere_counts_only_once_public() -> None:
    # Each of tonight's candidates dresses for SEA, a team not playing tonight, in a game not yet
    # public at the as-of time, so nobody drops out. Had it been public, each would.
    template = LINEUPS.filter(pl.col("game_date") == NIGHT, pl.col("role") != "G").head(1)
    elsewhere = pl.concat(
        [
            template.with_columns(
                player_id=pl.lit(player, dtype=pl.Int64),
                team=pl.lit("SEA"),
                game_id=pl.lit(-k, dtype=pl.Int64),
            )
            for k, player in enumerate(BEFORE["player_id"], start=1)
        ]
    )
    assert same(inputs(pl.concat([LINEUPS, elsewhere])), BEFORE)
    moment = TONIGHT.select(as_of(pl.col("game_date"), pl.col("start_utc"))).item(0, 0)
    early = elsewhere.with_columns(observed_utc=pl.lit(moment - timedelta(hours=1)))
    assert inputs(pl.concat([LINEUPS, early])).is_empty()


def test_a_renamed_teams_boxscore_counts_only_once_public() -> None:
    # UTA's first game follows ARI's last, through the line PHX, ARI, UTA. ARI's boxscore public
    # at UTA's as-of time is not read; a microsecond earlier, it is.
    lines = {"ARI": "PHX", "UTA": "PHX"}
    # Tonight's first game, played by UTA, with BOS's earlier boxscores as ARI's.
    target = TONIGHT.head(1).with_columns(home=pl.lit("UTA"), away=pl.lit("CHI"))
    moment = target.select(as_of(pl.col("game_date"), pl.col("start_utc"))).item()
    history = LINEUPS.filter(pl.col("game_date") < NIGHT, pl.col("team") == "BOS").with_columns(
        team=pl.lit("ARI"), game_id=-pl.col("game_id")
    )
    last = history["game_date"].max()

    def rated(observed: object) -> pl.DataFrame:
        late = history.with_columns(
            observed_utc=pl.when(pl.col("game_date") == last)
            .then(pl.lit(observed))
            .otherwise(pl.col("observed_utc"))
        )
        rows = pr.candidates(target, late, lines).drop(LABELS)
        return rows.filter(pl.col("team") == "UTA")

    without = pr.candidates(target, history.filter(pl.col("game_date") < last), lines)
    without = without.drop(LABELS).filter(pl.col("team") == "UTA")
    assert not without.is_empty()
    assert same(rated(moment), without)
    assert not same(rated(moment - timedelta(microseconds=1)), without)


def fitted(lineups: pl.DataFrame) -> np.ndarray:
    """The 2012-13 model's coefficients and expected newcomers."""
    rows = pr.candidates(GAMES, lineups, {})
    model = pr.fit(rows, GAMES, SEASON, VERSION)
    return np.asarray([*model.coefficients, *model.newcomers])


def test_the_seasons_own_boxscores_are_never_fitted_on() -> None:
    changed = altered(LINEUPS, pl.col("season") >= SEASON)
    np.testing.assert_allclose(fitted(changed), fitted(LINEUPS), rtol=0, atol=1e-12)


def test_an_earlier_boxscore_published_after_the_cutoff_is_not_fitted_on() -> None:
    # 2011-12's last night, as if its boxscores came out only after 2012-13's first as-of time
    # and named other skaters: the fit is the one without those boxscores at all.
    cutoff = gs.season_cutoff(GAMES, SEASON)
    last = GAMES.filter(pl.col("season") == SEASON - 10001)["game_date"].max()
    late = pl.col("game_date") == last
    published_late = altered(LINEUPS, late).with_columns(
        observed_utc=pl.when(late)
        .then(pl.lit(cutoff + timedelta(hours=1)))
        .otherwise(pl.col("observed_utc"))
    )
    left_out = LINEUPS.filter(~late)
    np.testing.assert_allclose(fitted(published_late), fitted(left_out), rtol=0, atol=1e-12)
    assert not np.allclose(fitted(altered(LINEUPS, late)), fitted(LINEUPS))


def test_every_probability_is_known_before_e1_and_e2_and_after_its_model() -> None:
    skaters, _, models = pr.score(LINEUPS, GAMES, [20112012, SEASON], VERSION, {})
    starts, _, _ = gs.score(LINEUPS, GAMES, [20112012, SEASON], "goalie-start-20261001-abc1234", {})
    table = pr.with_goalies(skaters, starts)
    for row in table.join(GAMES.select("game_id", "start_utc"), on="game_id").iter_rows(named=True):
        public = row["start_utc"] - timedelta(days=30)
        e2 = open_assumed_utc(row["game_date"], row["start_utc"], public) + PREDICTION_LAG
        assert row["train_cutoff"] < row["observed_utc"] < min(e2, row["start_utc"])
    for model in models:
        first = GAMES.filter(pl.col("season") == model.season)["start_utc"].min()
        assert model.train_cutoff < gs.season_cutoff(GAMES, model.season) <= first  # type: ignore[operator]
        rows = table.filter(pl.col("season") == model.season)
        # Nothing of the season is known before its models existed.
        assert known_at(rows, model.train_cutoff + timedelta(microseconds=1)).is_empty()
