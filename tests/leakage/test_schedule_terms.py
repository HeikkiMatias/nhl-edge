"""Point-in-time rules for the schedule terms and the season home edge (#77, ADR 0011, hard rule
1). A game's terms read the schedule as schedule_known_at does (ADR 0005): a team's last game
counts once its result is public (ADR 0003), tonight's own row only for its venue and flags. The
home edge reads results public before the as-of time. Tonight's result, later games and their
schedule rows never move them."""

from datetime import date, timedelta

import polars as pl
from polars.testing import assert_frame_equal
from schedule_fixtures import frames, game_row, league

from nhl_edge.backtest.market import PREDICTION_LAG
from nhl_edge.backtest.seasons import SeasonRole, season_role
from nhl_edge.features import schedule_terms as st
from nhl_edge.ingest.games import schedule_known_at
from nhl_edge.ingest.sbr import open_assumed_utc
from nhl_edge.lake.tables import known_at
from nhl_edge.reference import Reference

REF = Reference.load()
LEAGUE = league()
SETTINGS = st.Settings(prior_games=100)
SEASON = 20122013
NIGHT = LEAGUE["games"].filter(pl.col("season") == SEASON)["game_date"].unique().sort()[12]
TONIGHT = LEAGUE["schedule"].filter(pl.col("game_date") == NIGHT)["game_id"]
VERSION = "schedule-terms-20261001-abc1234"


def tonight(data: dict[str, pl.DataFrame]) -> pl.DataFrame:
    frame = st.terms(data["schedule"], data["games"], SETTINGS, [SEASON], REF)
    return frame.filter(pl.col("game_id").is_in(TONIGHT.implode()))


BEFORE = tonight(LEAGUE)


def same(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    try:
        assert_frame_equal(left, right, rel_tol=1e-12, abs_tol=1e-12)
    except AssertionError:
        return False
    return True


def flipped(rows: pl.Expr) -> dict[str, pl.DataFrame]:
    """The league with the chosen games' results reversed."""
    chosen = LEAGUE["games"].filter(rows)["game_id"].implode()
    pick = pl.col("game_id").is_in(chosen)
    games = LEAGUE["games"].with_columns(
        home_score=pl.when(pick).then(pl.col("away_score")).otherwise(pl.col("home_score")),
        away_score=pl.when(pick).then(pl.col("home_score")).otherwise(pl.col("away_score")),
    )
    return {**LEAGUE, "games": games}


def test_tonights_and_later_results_never_move_tonights_terms() -> None:
    assert same(tonight(flipped(pl.col("game_date") >= NIGHT)), BEFORE)


def test_earlier_results_do_move_the_home_edge() -> None:
    # The guard above is not vacuous.
    assert not same(tonight(flipped(pl.col("game_date") < NIGHT)), BEFORE)


def test_later_games_missing_from_the_schedule_never_move_them() -> None:
    # A later game postponed out of the schedule, or moved: tonight cannot know.
    later = pl.col("game_date") > NIGHT
    cut = {name: frame.filter(~later) for name, frame in LEAGUE.items()}
    assert same(tonight(cut), BEFORE)


def test_each_teams_last_game_is_the_one_schedule_known_at_returns() -> None:
    schedule = LEAGUE["schedule"]
    frame = st.terms(schedule, LEAGUE["games"], SETTINGS, [SEASON], REF)
    for row in frame.sample(25, seed=2).iter_rows(named=True):
        known = schedule_known_at(schedule, row["as_of_utc"], [row["game_id"]])
        for side in ("home", "away"):
            team = row[side]
            earlier = known.filter(
                (pl.col("home") == team) | (pl.col("away") == team),
                pl.col("season") == SEASON,
                pl.col("game_id") != row["game_id"],
            )
            last = earlier["game_date"].max()
            rest = 4 if last is None else min((row["game_date"] - last).days, 4)  # type: ignore[operator]
            assert row[f"{side}_rest_days"] == rest


def test_a_result_public_at_the_as_of_time_is_not_read() -> None:
    moment = BEFORE["as_of_utc"][0]
    last_night = LEAGUE["games"].filter(pl.col("game_date") < NIGHT)["game_date"].max()
    late = pl.col("game_date") == last_night

    def public_at(when: object) -> dict[str, pl.DataFrame]:
        observed = pl.when(late).then(pl.lit(when)).otherwise(pl.col("observed_utc"))
        return {**LEAGUE, "games": LEAGUE["games"].with_columns(observed_utc=observed)}

    edge = ["game_id", "home_win_rate", "h_s", "season_games"]
    dropped = {**LEAGUE, "games": LEAGUE["games"].filter(~late)}
    assert same(tonight(public_at(moment)).select(edge), tonight(dropped).select(edge))
    assert same(
        tonight(public_at(moment - timedelta(microseconds=1))).select(edge), BEFORE.select(edge)
    )
    assert not same(tonight(dropped).select(edge), BEFORE.select(edge))


def test_every_row_is_known_before_e1_and_e2_and_after_the_cutoff() -> None:
    frame = st.terms(LEAGUE["schedule"], LEAGUE["games"], SETTINGS, [20112012, SEASON], REF)
    out = st.rows(frame, SETTINGS, VERSION)
    starts = LEAGUE["schedule"].select("game_id", "start_utc", public="observed_utc")
    for row in out.join(starts, on="game_id").iter_rows(named=True):
        e2 = open_assumed_utc(row["game_date"], row["start_utc"], row["public"]) + PREDICTION_LAG
        assert row["as_of_utc"] < min(e2, row["start_utc"])
        assert row["observed_utc"] == max(row["as_of_utc"], st.TUNED_CUTOFF)
    assert known_at(out, st.TUNED_CUTOFF).is_empty()


def test_tuning_reads_the_training_seasons_only() -> None:
    assert st.TUNING_SEASONS
    assert all(season_role(s) is SeasonRole.TRAINING for s in st.TUNING_SEASONS)
    assert max(st.TUNING_SEASONS) == 20172018


def test_a_game_retimed_on_the_day_is_rated_once_its_schedule_is_public() -> None:
    # As the Lake Tahoe game (ADR 0005): tonight's first game's row public only at 20:00 UTC on
    # the day, after the 10:00 ET as-of time but before its 23:00 UTC start.
    game = TONIGHT[0]
    row = LEAGUE["schedule"].filter(pl.col("game_id") == game).row(0, named=True)
    public = row["start_utc"] - timedelta(hours=3)
    schedule = LEAGUE["schedule"].with_columns(
        observed_utc=pl.when(pl.col("game_id") == game)
        .then(pl.lit(public))
        .otherwise(pl.col("observed_utc"))
    )
    rated = tonight({**LEAGUE, "schedule": schedule})
    retimed = rated.filter(pl.col("game_id") == game).row(0, named=True)
    assert retimed["as_of_utc"] == public + timedelta(microseconds=1) < row["start_utc"]
    # E2 waits for the moved schedule too (the opener's assumed time), so it can still read it.
    e2 = open_assumed_utc(row["game_date"], row["start_utc"], public) + PREDICTION_LAG
    assert retimed["as_of_utc"] < e2
    # The other games tonight are rated as before.
    others = pl.col("game_id") != game
    assert same(rated.filter(others), BEFORE.filter(others))


def test_a_seat_limit_is_read_only_once_announced() -> None:
    # Florida's 25% from 2021-01-13 was announced on 2021-01-06, public the next morning (ADR
    # 0003's rule). Announced on the game day instead, it is not known yet: the arena counts as
    # full.
    data = frames([game_row(1, 20202021, date(2021, 2, 1), "FLA", "BOS", "BB&T Center")])
    late = Reference(
        **{
            **REF.__dict__,
            "attendance_limits": REF.attendance_limits.with_columns(
                announced=pl.when(pl.col("arena_id") == "amerant_bank_arena")
                .then(pl.lit(date(2021, 2, 1)))
                .otherwise(pl.col("announced"))
            ),
        }
    )

    def share(ref: Reference) -> float:
        frame = st.terms(data["schedule"], data["games"], SETTINGS, [20202021], ref)
        return frame["capacity_share"].item()

    assert share(REF) == 0.25
    assert share(late) == 1.0
