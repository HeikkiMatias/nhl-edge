import math
from datetime import UTC, date, datetime
from typing import Any

import polars as pl
import pytest
from schedule_fixtures import NEUTRAL_VENUE, frames, game_row, league

from nhl_edge.features import schedule_terms as st
from nhl_edge.lake.schemas import ScheduleTerms
from nhl_edge.reference import Reference

REF = Reference.load()
LEAGUE = league()
VERSION = "schedule-terms-20261001-abc1234"
SETTINGS = st.Settings(prior_games=100)
# Great-circle distances between the arenas, in km, from their coordinates in arenas.csv.
BOS_LAK = 4_172
BOS_NYR = 303


def rated(
    rows: list[dict[str, object]], seasons: list[int] | None = None
) -> dict[int, dict[str, Any]]:
    data = frames(rows)
    frame = st.terms(data["schedule"], data["games"], SETTINGS, seasons or [20112012], REF)
    return {row["game_id"]: row for row in frame.iter_rows(named=True)}


# BOS opens at NYR, comes home, flies to Los Angeles the next night, then rests five days.
TRIP = [
    game_row(1, 20112012, date(2011, 10, 6), "NYR", "BOS"),
    game_row(2, 20112012, date(2011, 10, 8), "BOS", "NYR"),
    game_row(3, 20112012, date(2011, 10, 9), "LAK", "BOS"),
    game_row(4, 20112012, date(2011, 10, 14), "BOS", "LAK"),
]


def test_a_seasons_first_game_travels_from_the_home_arena_after_full_rest() -> None:
    first = rated(TRIP)[1]
    assert (first["away_rest_days"], first["away_back_to_back"]) == (4, False)
    assert first["away_travel_km"] == pytest.approx(BOS_NYR, abs=2)
    assert first["away_tz_shift"] == 0.0
    # NYR plays its first game at home: no travel.
    assert (first["home_rest_days"], first["home_travel_km"]) == (4, 0.0)


def test_rest_back_to_backs_travel_and_time_zones() -> None:
    games = rated(TRIP)
    assert (games[2]["home_rest_days"], games[2]["home_travel_km"]) == (
        2,
        pytest.approx(BOS_NYR, abs=2),
    )
    trip = games[3]
    assert (trip["away_rest_days"], trip["away_back_to_back"]) == (1, True)
    assert trip["away_travel_km"] == pytest.approx(BOS_LAK, abs=5)
    assert trip["away_tz_shift"] == -3.0  # Boston to Los Angeles: three hours west
    back = games[4]
    assert back["home_rest_days"] == 4  # five days, capped
    assert back["home_tz_shift"] == 3.0
    # LAK's previous game was at home the 9th: it travels east too.
    assert (back["away_rest_days"], back["away_tz_shift"]) == (4, 3.0)


def test_a_neutral_site_abroad() -> None:
    rows = [
        game_row(1, 20112012, date(2011, 11, 8), "BOS", "NYR"),
        game_row(2, 20112012, date(2011, 11, 11), "NYR", "BOS", NEUTRAL_VENUE, neutral=True),
    ]
    game = rated(rows)[2]
    assert game["neutral_site"] is True
    # Both teams left the Eastern time zone for Stockholm: six hours east in November.
    assert (game["home_tz_shift"], game["away_tz_shift"]) == (6.0, 6.0)
    assert game["home_travel_km"] > 5_000 and game["away_travel_km"] > 5_000


def test_capacity_share_as_announced() -> None:
    # Florida's arena opened at 25% from 2021-01-13, announced 2021-01-06; Boston's stayed empty
    # until 2021-03-22, and the 12% that followed was announced on 2021-02-25.
    rows = [
        game_row(1, 20202021, date(2021, 2, 1), "FLA", "BOS", "BB&T Center"),
        game_row(2, 20202021, date(2021, 2, 3), "BOS", "FLA"),
        game_row(3, 20202021, date(2021, 3, 25), "BOS", "FLA"),
    ]
    games = rated(rows, [20202021])
    assert [games[g]["capacity_share"] for g in (1, 2, 3)] == [0.25, 0.0, 0.12]


def test_the_home_edge_is_pulled_toward_the_three_seasons_before() -> None:
    rows = []
    # Three earlier seasons, 10 games each: home wins 6, 7 and 8 of them. A season further back
    # (8 home wins of 10) is not read.
    for season, wins in ((20072008, 8), (20082009, 6), (20092010, 7), (20102011, 8)):
        for k in range(10):
            day = date(season // 10000, 11, 1 + k)
            home_score, away_score = (3, 1) if k < wins else (1, 3)
            rows.append(
                game_row(season * 100 + k, season, day, "BOS", "NYR", None, home_score, away_score)
            )
    # This season: two home losses, then the game to rate.
    for k, day in enumerate((date(2011, 11, 1), date(2011, 11, 3))):
        rows.append(game_row(1000 + k, 20112012, day, "BOS", "NYR", None, 1, 3))
    rows.append(game_row(2000, 20112012, date(2011, 11, 5), "BOS", "NYR"))
    # A neutral-site home win this season does not count.
    rows.append(
        game_row(1500, 20112012, date(2011, 11, 4), "NYR", "BOS", NEUTRAL_VENUE, 3, 1, neutral=True)
    )
    game = rated(rows)[2000]
    prior = (6 + 7 + 8) / 30
    expected = (0 + 100 * prior) / (2 + 100)
    assert game["season_games"] == 2
    assert game["home_win_rate"] == pytest.approx(expected)
    assert game["h_s"] == pytest.approx(math.log(expected / (1 - expected)))


def test_rows_validate_and_carry_the_cutoff() -> None:
    frame = st.terms(LEAGUE["schedule"], LEAGUE["games"], SETTINGS, [20112012, 20122013], REF)
    out = st.rows(frame, SETTINGS, VERSION)
    ScheduleTerms.validate(out)
    assert out.height == LEAGUE["schedule"].filter(pl.col("season") >= 20112012).height
    assert (out["observed_utc"] == st.TUNED_CUTOFF).all()  # the fixture predates the cutoff
    later = st.rows(frame, SETTINGS, VERSION, train_cutoff=datetime(2010, 1, 1, tzinfo=UTC))
    assert (later["observed_utc"] == later["as_of_utc"]).all()


def test_rows_of_another_season_s_game_are_refused() -> None:
    # #131: B3 trusts season to keep each fold to its own rows and cutoffs.
    frame = st.terms(LEAGUE["schedule"], LEAGUE["games"], SETTINGS, [20112012], REF)
    out = st.rows(frame, SETTINGS, VERSION).head(1)
    with pytest.raises(Exception, match="regular_season_id_of_its_season"):
        ScheduleTerms.validate(out.with_columns(season=pl.col("season") + 10_001))


def test_the_tuning_feature_is_zero_at_a_neutral_site() -> None:
    frame = st.terms(LEAGUE["schedule"], LEAGUE["games"], SETTINGS, [20112012], REF)
    feature = st.tuning_feature(frame).join(
        frame.select("game_id", "neutral_site", "h_s"), on="game_id"
    )
    assert (feature.filter("neutral_site")["x"] == 0).all()
    plain = feature.filter(~pl.col("neutral_site"))
    assert (plain["x"] == plain["h_s"]).all()


def test_the_grid_and_its_steadiness_order() -> None:
    assert len(st.GRID) == 6 and st.TUNED in st.GRID
    assert max(st.GRID, key=st.steadiness).prior_games == 1600


def test_input_problems() -> None:
    schedule, games = LEAGUE["schedule"], LEAGUE["games"]
    counts = dict(games.group_by("season").len().iter_rows())
    assert st.input_problems(schedule, games, 20122013, counts, REF) == []
    first = schedule.filter(pl.col("season") == 20112012)["game_id"][0]
    unknown = schedule.with_columns(
        venue=pl.when(pl.col("game_id") == first)
        .then(pl.lit("Nowhere Arena"))
        .otherwise(pl.col("venue"))
    )
    assert st.input_problems(unknown, games, 20122013, counts, REF) == [
        f"1 games without an arena, e.g. {first}"
    ]
    rows = frames([game_row(1, 20112012, date(2011, 10, 8), "BOS", "SEA", "TD Garden")])
    assert st.input_problems(rows["schedule"], rows["games"], 20112012, {}, REF) == [
        "20112012: SEA has no primary home arena"
    ]
