"""Point-in-time rules for team strength (#74, ADR 0011, hard rule 1). A game's ΔS reads only
team-games public before 10:00 US Eastern on its date (or its start, if earlier): its own game,
later games and anything public after then never move it, for its two teams or for the league
average it shrinks toward."""

from datetime import UTC, datetime, timedelta

import polars as pl
from team_fixtures import league

from nhl_edge.backtest import tuning
from nhl_edge.backtest.seasons import SeasonRole, season_role
from nhl_edge.features import team_strength as ts
from nhl_edge.ingest.sbr import open_assumed_utc

LEAGUE = league()
SETTINGS = ts.Settings(half_life=20, prior_games=10)
# A night in the second season, with every earlier night's games behind it.
NIGHT = LEAGUE["games"].filter(pl.col("season") == 20122013)["game_date"].unique().sort()[10]


def delta(inputs: dict[str, pl.DataFrame]) -> pl.DataFrame:
    history = ts.team_games(inputs["shots"], inputs["shot_xg"], inputs["strength_time"])
    tonight = inputs["games"].filter(pl.col("game_date") == NIGHT)
    return ts.strength(tonight, history, SETTINGS).select("game_id", "delta_s", "home_history")


BEFORE = delta(LEAGUE)


def scaled_xg(rows: pl.Expr, factor: float) -> dict[str, pl.DataFrame]:
    """The league with the xG of the chosen games' shots multiplied by factor."""
    chosen = LEAGUE["games"].filter(rows).select("game_id")
    xg = LEAGUE["shot_xg"].with_columns(
        xg=pl.when(pl.col("game_id").is_in(chosen["game_id"].implode()))
        .then(pl.col("xg") * factor)
        .otherwise(pl.col("xg"))
    )
    return {**LEAGUE, "shot_xg": xg}


def test_tonights_and_later_games_never_move_tonights_ratings() -> None:
    changed = scaled_xg(pl.col("game_date") >= NIGHT, 3.0)
    assert delta(changed).equals(BEFORE)


def test_earlier_games_do_move_them() -> None:
    # The guard above is not vacuous.
    changed = scaled_xg(pl.col("game_date") < NIGHT, 3.0)
    assert not delta(changed).equals(BEFORE)


def test_a_game_public_only_after_ten_eastern_is_not_read() -> None:
    # The previous night's games (two days back in this league), as if their feeds came out only
    # after 10:00 ET tonight: they drop out of both teams' histories and the league average,
    # exactly as if never played.
    last_night = LEAGUE["games"].filter(pl.col("game_date") < NIGHT)["game_date"].max()
    late = pl.col("game_date") == last_night
    shifted = {
        **LEAGUE,
        "strength_time": LEAGUE["strength_time"].with_columns(
            observed_utc=pl.when(late)
            .then(pl.col("observed_utc") + timedelta(days=2))
            .otherwise(pl.col("observed_utc"))
        ),
    }
    dropped = {
        **LEAGUE,
        "games": LEAGUE["games"].filter(~late),
        "strength_time": LEAGUE["strength_time"].filter(~late),
    }
    assert (
        delta(shifted)
        .select("game_id", "delta_s")
        .equals(delta(dropped).select("game_id", "delta_s"))
    )


def test_every_rating_is_public_before_its_game_starts() -> None:
    history = ts.team_games(LEAGUE["shots"], LEAGUE["shot_xg"], LEAGUE["strength_time"])
    frame = ts.rows(LEAGUE["games"], history, ts.TUNED, "team-strength-20261001-abc1234")
    starts = LEAGUE["games"].select("game_id", "start_utc")
    joined = frame.join(starts, on="game_id")
    assert (joined["observed_utc"] <= joined["start_utc"]).all()
    # Every team-game a rating read was public before it: the history counts match.
    public = history.select("team", "observed_utc")
    for row in joined.sample(20, seed=1).iter_rows(named=True):
        seen = public.filter(
            pl.col("team") == row["home"], pl.col("observed_utc") < row["observed_utc"]
        ).height
        assert row["home_history"] == seen


# The tuned settings and the exact cutoff (leakage check on #74)


def test_rows_carry_the_tuning_runs_cutoff() -> None:
    # Ratings observed before it used settings chosen with their own season's results: every
    # training season, by design (ADR 0011). The development seasons' folds start after it.
    history = ts.team_games(LEAGUE["shots"], LEAGUE["shot_xg"], LEAGUE["strength_time"])
    frame = ts.rows(LEAGUE["games"], history, ts.TUNED, "team-strength-20261001-abc1234")
    assert set(frame["train_cutoff"]) == {ts.TUNED_CUTOFF}
    first_2018_19 = datetime(2018, 10, 3, 23, tzinfo=UTC)  # 2018-19 began on 2018-10-03
    assert first_2018_19 > ts.TUNED_CUTOFF


def test_tuning_reads_the_training_seasons_only() -> None:
    assert ts.TUNING_SEASONS
    assert all(season_role(s) is SeasonRole.TRAINING for s in ts.TUNING_SEASONS)
    assert max(ts.TUNING_SEASONS) == 20172018


def test_every_rating_is_public_before_e2_and_e1_predict() -> None:
    history = ts.team_games(LEAGUE["shots"], LEAGUE["shot_xg"], LEAGUE["strength_time"])
    frame = ts.rows(LEAGUE["games"], history, ts.TUNED, "team-strength-20261001-abc1234")
    for row in frame.join(LEAGUE["games"].select("game_id", "start_utc"), on="game_id").iter_rows(
        named=True
    ):
        # E2 predicts at 10:00 ET or later, and E1 at the start.
        e2 = open_assumed_utc(
            row["game_date"], row["start_utc"], row["start_utc"] - timedelta(days=30)
        )
        assert row["observed_utc"] <= e2 <= row["start_utc"]


def test_a_team_game_public_exactly_at_the_cutoff_is_not_read() -> None:
    tonight = LEAGUE["games"].filter(pl.col("game_date") == NIGHT)
    cutoff = tonight.select(ts.as_of(pl.col("game_date"), pl.col("start_utc"))).item(0, 0)
    last_night = LEAGUE["games"].filter(pl.col("game_date") < NIGHT)["game_date"].max()
    late = pl.col("game_date") == last_night

    def public_at(moment: datetime) -> dict[str, pl.DataFrame]:
        observed = pl.when(late).then(pl.lit(moment)).otherwise(pl.col("observed_utc"))
        return {
            **LEAGUE,
            "strength_time": LEAGUE["strength_time"].with_columns(observed_utc=observed),
            "shot_xg": LEAGUE["shot_xg"].with_columns(
                observed_utc=pl.when(
                    pl.col("game_id").is_in(LEAGUE["games"].filter(late)["game_id"].implode())
                )
                .then(pl.lit(moment))
                .otherwise(pl.col("observed_utc"))
            ),
        }

    dropped = {
        **LEAGUE,
        "games": LEAGUE["games"].filter(~late),
        "strength_time": LEAGUE["strength_time"].filter(~late),
    }
    at = delta(public_at(cutoff)).select("game_id", "delta_s")
    just_before = delta(public_at(cutoff - timedelta(microseconds=1))).select("game_id", "delta_s")
    assert at.equals(delta(dropped).select("game_id", "delta_s"))
    assert just_before.equals(BEFORE.select("game_id", "delta_s"))


def test_an_earlier_result_published_after_the_fold_start_is_not_fitted_on() -> None:
    # tuning.scored_games fits each season on results public before its first game: a 2011-12
    # result published only after 2012-13 began is left out, whatever it says.
    history = ts.team_games(LEAGUE["shots"], LEAGUE["shot_xg"], LEAGUE["strength_time"])
    feature = ts.strength(LEAGUE["games"], history, SETTINGS).select("game_id", x="delta_s")
    games = LEAGUE["games"]
    late_game = games.filter(pl.col("season") == 20112012)["game_id"][-1]
    start = games.filter(pl.col("season") == 20122013)["start_utc"].min()
    assert isinstance(start, datetime)
    late = pl.col("game_id") == late_game
    published_late = games.with_columns(
        observed_utc=pl.when(late)
        .then(pl.lit(start + timedelta(days=1)))
        .otherwise(pl.col("observed_utc")),
        home_score=pl.when(late).then(pl.lit(9)).otherwise(pl.col("home_score")),
        away_score=pl.when(late).then(pl.lit(0)).otherwise(pl.col("away_score")),
    )
    left_out = games.filter(~late)
    scored = tuning.scored_games(feature, published_late, [20122013])
    assert scored.equals(tuning.scored_games(feature, left_out, [20122013]))
