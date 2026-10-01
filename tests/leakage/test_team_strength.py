"""Point-in-time rules for team strength (#74, ADR 0011, hard rule 1). A game's ΔS reads only
team-games public before 10:00 US Eastern on its date (or its start, if earlier): its own game,
later games and anything public after then never move it, for its two teams or for the league
average it shrinks toward."""

from datetime import timedelta

import polars as pl
from team_fixtures import league

from nhl_edge.features import team_strength as ts

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
