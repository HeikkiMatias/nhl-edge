"""A small league for the uncertainty score u (#139): two games of one team pair, with explicit
goalie, lineup, replacement, boxscore and career rows."""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import polars as pl

from nhl_edge.game import uncertainty as un

UTC_TYPE = pl.Datetime("us", "UTC")
SEASON = 20212022
DAY = date(2021, 11, 10)
AS_OF = datetime(2021, 11, 10, 15, 0, tzinfo=UTC)
PREDICTION = datetime(2021, 11, 10, 15, 0, 1, tzinfo=UTC)
GAME = 2021020200


def frame(rows: list[dict[str, object]], schema: Mapping[str, Any]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=schema, orient="row")


GAMES = frame(
    [{"game_id": GAME, "season": SEASON, "home": "BOS", "away": "TOR", "game_date": DAY}],
    {
        "game_id": pl.Int64,
        "season": pl.Int32,
        "home": pl.String,
        "away": pl.String,
        "game_date": pl.Date,
    },
)
GOALIES = frame(
    [
        {"game_id": GAME, "team": "BOS", "goalie_id": 1, "p_start": 0.6, "observed_utc": AS_OF},
        {"game_id": GAME, "team": "BOS", "goalie_id": 2, "p_start": 0.4, "observed_utc": AS_OF},
        {"game_id": GAME, "team": "TOR", "goalie_id": 3, "p_start": 1.0, "observed_utc": AS_OF},
    ],
    {
        "game_id": pl.Int64,
        "team": pl.String,
        "goalie_id": pl.Int64,
        "p_start": pl.Float64,
        "observed_utc": UTC_TYPE,
    },
)
# BOS: a veteran (101), a second-season skater with 70 earlier games (102), a doubtful one (103).
# TOR: one regular (201).
LINEUPS = frame(
    [
        {
            "game_id": GAME,
            "team": "BOS",
            "player_id": 101,
            "role": "F",
            "p_available": 1.0,
            "exp_5v5": 15.0,
            "observed_utc": AS_OF,
        },
        {
            "game_id": GAME,
            "team": "BOS",
            "player_id": 102,
            "role": "D",
            "p_available": 1.0,
            "exp_5v5": 20.0,
            "observed_utc": AS_OF,
        },
        {
            "game_id": GAME,
            "team": "BOS",
            "player_id": 103,
            "role": "F",
            "p_available": 0.5,
            "exp_5v5": 5.0,
            "observed_utc": AS_OF,
        },
        {
            "game_id": GAME,
            "team": "TOR",
            "player_id": 201,
            "role": "F",
            "p_available": 0.9,
            "exp_5v5": 30.0,
            "observed_utc": AS_OF,
        },
        {
            "game_id": GAME,
            "team": "TOR",
            "player_id": 3,
            "role": "G",
            "p_available": None,
            "exp_5v5": None,
            "observed_utc": AS_OF,
        },
    ],
    {
        "game_id": pl.Int64,
        "team": pl.String,
        "player_id": pl.Int64,
        "role": pl.String,
        "p_available": pl.Float64,
        "exp_5v5": pl.Float64,
        "observed_utc": UTC_TYPE,
    },
)
SPARE = frame(
    [{"game_id": GAME, "team": "TOR", "exp_5v5": 10.0, "observed_utc": AS_OF}],
    {"game_id": pl.Int64, "team": pl.String, "exp_5v5": pl.Float64, "observed_utc": UTC_TYPE},
)
BOX_SCHEMA = {
    "game_id": pl.Int64,
    "season": pl.Int32,
    "team": pl.String,
    "player_id": pl.Int64,
    "role": pl.String,
    "observed_utc": UTC_TYPE,
}
# 102 dressed in 12 earlier games this season, the last public the morning of the game.
BOXSCORES = frame(
    [
        {
            "game_id": 2021020100 + k,
            "season": SEASON,
            "team": "BOS",
            "player_id": 102,
            "role": "D",
            "observed_utc": datetime(2021, 10, 20 + k, 10, tzinfo=UTC),
        }
        for k in range(1, 11)
    ]
    + [
        {
            "game_id": 2021020190,
            "season": SEASON,
            "team": "BOS",
            "player_id": 102,
            "role": "D",
            "observed_utc": datetime(2021, 11, 9, 10, tzinfo=UTC),
        },
        {
            "game_id": 2021020195,
            "season": SEASON,
            "team": "BOS",
            "player_id": 102,
            "role": "D",
            "observed_utc": datetime(2021, 11, 10, 10, tzinfo=UTC),
        },
    ],
    BOX_SCHEMA,
)
LINE_SCHEMA = {
    "player_id": pl.Int64,
    "season": pl.Int32,
    "league": pl.String,
    "game_type": pl.Int8,
    "games_played": pl.Int16,
    "observed_utc": UTC_TYPE,
}
LINES = frame(
    [
        # 101: 300 NHL games over earlier seasons.
        {
            "player_id": 101,
            "season": 20192020,
            "league": "NHL",
            "game_type": 2,
            "games_played": 150,
            "observed_utc": datetime(2020, 10, 1, tzinfo=UTC),
        },
        {
            "player_id": 101,
            "season": 20202021,
            "league": "NHL",
            "game_type": 2,
            "games_played": 150,
            "observed_utc": datetime(2021, 7, 9, tzinfo=UTC),
        },
        # 102: 58 NHL games last season, and AHL and playoff games that do not count.
        {
            "player_id": 102,
            "season": 20202021,
            "league": "NHL",
            "game_type": 2,
            "games_played": 58,
            "observed_utc": datetime(2021, 7, 9, tzinfo=UTC),
        },
        {
            "player_id": 102,
            "season": 20202021,
            "league": "NHL",
            "game_type": 3,
            "games_played": 20,
            "observed_utc": datetime(2021, 7, 9, tzinfo=UTC),
        },
        {
            "player_id": 102,
            "season": 20192020,
            "league": "AHL",
            "game_type": 2,
            "games_played": 60,
            "observed_utc": datetime(2020, 10, 1, tzinfo=UTC),
        },
        # 201: 100 NHL games.
        {
            "player_id": 201,
            "season": 20202021,
            "league": "NHL",
            "game_type": 2,
            "games_played": 100,
            "observed_utc": datetime(2021, 7, 9, tzinfo=UTC),
        },
    ],
    LINE_SCHEMA,
)
MOMENTS = frame(
    [{"game_id": GAME, "prediction_utc": PREDICTION}],
    {"game_id": pl.Int64, "prediction_utc": UTC_TYPE},
)


def tables(**replaced: pl.DataFrame) -> un.Tables:
    base = {
        "games": GAMES,
        "goalie_starts": GOALIES,
        "lineups": LINEUPS,
        "lineup_replacements": SPARE,
        "actual_lineups": BOXSCORES,
        "player_league_seasons": LINES,
    }
    return un.Tables(**(base | replaced))
