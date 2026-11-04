"""Point in time for features/density.py (#370, ADR 0056, hard rule 1): a game's density counts
only the season's earlier games whose results were public before its prediction time, never
tonight's game, another on tonight's date, a later game or another season's."""

from datetime import date, timedelta

import polars as pl
from targeted_features_fixtures import moments, schedule

from nhl_edge.features.density import DENSITY, density, team_games

D = date(2013, 11, 1)
BASE = [(1, D, "NYR", "BOS"), (2, D + timedelta(days=1), "PHI", "BOS")]


def of(games: pl.DataFrame, game_id: int = 2) -> dict[str, float]:
    return density(games, moments(games)).filter(pl.col("game_id") == game_id).row(0, named=True)


def test_tonight_another_game_tonight_and_later_games_are_never_counted() -> None:
    before = of(schedule(BASE))
    later = schedule([*BASE, (3, D + timedelta(days=2), "BOS", "PHI")])
    same_day = schedule([*BASE, (4, D + timedelta(days=1), "NYR", "WSH")])
    assert of(later) == before and of(same_day) == before


def test_a_result_public_only_after_the_prediction_is_not_counted() -> None:
    late = schedule(BASE).with_columns(
        observed_utc=pl.when(pl.col("game_id") == 1)
        .then(pl.col("observed_utc") + timedelta(hours=6))
        .otherwise(pl.col("observed_utc"))
    )
    assert all(of(late)[c] == 0 for c in DENSITY)
    # On time, the same game counts: the check has power.
    on_time = of(schedule(BASE))
    assert (on_time["dense3_diff"], on_time["mismatch"], on_time["away_trip"]) == (-1, 1, 1)
    # A game without a public result (null observed_utc), such as tonight's slate live, neither.
    unknown = schedule(BASE).with_columns(
        observed_utc=pl.when(pl.col("game_id") == 1).then(None).otherwise(pl.col("observed_utc"))
    )
    assert all(of(unknown)[c] == 0 for c in DENSITY)


def test_another_seasons_games_are_never_counted() -> None:
    earlier = schedule([(1, D - timedelta(days=200), "NYR", "BOS")], season=20122013)
    games = pl.concat([earlier, schedule(BASE[1:])])
    per = team_games(games, moments(games).filter(pl.col("game_id") == 2))
    assert per["before"].to_list() == [0, 0]
