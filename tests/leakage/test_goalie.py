"""Point-in-time rules for the goalie effect (#75, ADR 0011, hard rule 1). A candidate's effect
and the shots his team is expected to allow read only goalie-games and team-games public before
the game's as-of time: 10:00 US Eastern on its date, or an hour before its start if that is
earlier. Tonight's shots, later ones, and who actually started tonight never move them."""

from datetime import timedelta

import polars as pl
from goalie_fixtures import league, with_shots
from polars.testing import assert_frame_equal

from nhl_edge.backtest.market import PREDICTION_LAG
from nhl_edge.backtest.seasons import SeasonRole, season_role
from nhl_edge.features import goalie as ge
from nhl_edge.features.team_strength import as_of
from nhl_edge.ingest.sbr import open_assumed_utc
from nhl_edge.lake.tables import known_at
from nhl_edge.lineup import goalie_start as gs

LEAGUE = with_shots(league())
GAMES = LEAGUE["games"]
STARTS, _, _ = gs.score(
    LEAGUE["lineups"], GAMES, [20112012, 20122013], "goalie-start-20261001-abc1234", {}
)
SETTINGS = ge.Settings(half_life=20, prior_shots=200)
# A night in 2012-13, with every earlier night's games behind it.
NIGHT = GAMES.filter(pl.col("season") == 20122013)["game_date"].unique().sort()[10]
TONIGHT = GAMES.filter(pl.col("game_date") == NIGHT)
VERSION = "goalie-effect-20261001-abc1234"


def effects(inputs: dict[str, pl.DataFrame], games: pl.DataFrame = TONIGHT) -> pl.DataFrame:
    goalies = ge.goalie_games(inputs["shots"], inputs["shot_xg"])
    team_shots = ge.team_shot_games(inputs["games"], inputs["shots"], inputs["shot_xg"])
    return ge.effects(STARTS, games, goalies, team_shots, SETTINGS, {})


BEFORE = effects(LEAGUE)


def same(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    """Equal up to float rounding: a leak moves an effect by far more than 1e-12."""
    try:
        assert_frame_equal(left, right, rel_tol=1e-12, abs_tol=1e-12)
    except AssertionError:
        return False
    return True


def changed(rows: pl.Expr) -> dict[str, pl.DataFrame]:
    """The league with the chosen games' shots all goals and their xG tripled."""
    chosen = GAMES.filter(rows)["game_id"].implode()
    pick = pl.col("game_id").is_in(chosen)
    return {
        **LEAGUE,
        "shots": LEAGUE["shots"].with_columns(
            is_goal=pl.when(pick).then(True).otherwise(pl.col("is_goal"))
        ),
        "shot_xg": LEAGUE["shot_xg"].with_columns(
            xg=pl.when(pick).then(pl.col("xg") * 3).otherwise(pl.col("xg"))
        ),
    }


def test_tonights_and_later_shots_never_move_tonights_effects() -> None:
    assert same(effects(changed(pl.col("game_date") >= NIGHT)), BEFORE)


def test_earlier_shots_do_move_them() -> None:
    # The guard above is not vacuous.
    assert not same(effects(changed(pl.col("game_date") < NIGHT)), BEFORE)


def test_who_started_tonight_never_moves_them() -> None:
    # Tonight's shots credited to the other goalie: tonight's own starter is never read.
    tonight = pl.col("game_id").is_in(TONIGHT["game_id"].implode())
    swapped = {
        **LEAGUE,
        "shots": LEAGUE["shots"].with_columns(
            goalie_id=pl.when(tonight).then(pl.col("goalie_id") + 1).otherwise(pl.col("goalie_id"))
        ),
    }
    assert same(effects(swapped), BEFORE)


def test_a_game_public_at_the_as_of_time_is_not_read() -> None:
    moment = TONIGHT.select(as_of(pl.col("game_date"), pl.col("start_utc"))).item(0, 0)
    last_night = GAMES.filter(pl.col("game_date") < NIGHT)["game_date"].max()
    late = pl.col("game_date") == last_night

    def public_at(when: object) -> dict[str, pl.DataFrame]:
        return {
            **LEAGUE,
            "shot_xg": LEAGUE["shot_xg"].with_columns(
                observed_utc=pl.when(late).then(pl.lit(when)).otherwise(pl.col("observed_utc"))
            ),
        }

    dropped = {
        **LEAGUE,
        "games": GAMES.filter(~late),
        "shot_xg": LEAGUE["shot_xg"].filter(~late),
    }
    assert same(effects(public_at(moment)), effects(dropped))
    assert same(effects(public_at(moment - timedelta(microseconds=1))), BEFORE)
    assert not same(effects(dropped), BEFORE)


def test_every_effect_is_known_before_e1_and_e2_and_after_the_cutoff() -> None:
    frame = ge.rows(effects(LEAGUE, GAMES.filter(pl.col("season") >= 20112012)), SETTINGS, VERSION)
    starts = GAMES.select("game_id", "start_utc")
    for row in frame.join(starts, on="game_id").iter_rows(named=True):
        public = row["start_utc"] - timedelta(days=30)
        e2 = open_assumed_utc(row["game_date"], row["start_utc"], public) + PREDICTION_LAG
        assert row["as_of_utc"] < min(e2, row["start_utc"])
        # The tuned seasons' effects count as known only from the tuning run's cutoff.
        assert row["observed_utc"] == max(row["as_of_utc"], ge.TUNED_CUTOFF)
    assert known_at(frame, ge.TUNED_CUTOFF).is_empty()


def test_tuning_reads_the_training_seasons_only() -> None:
    assert ge.TUNING_SEASONS
    assert all(season_role(s) is SeasonRole.TRAINING for s in ge.TUNING_SEASONS)
    assert max(ge.TUNING_SEASONS) == 20172018
