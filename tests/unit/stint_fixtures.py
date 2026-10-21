"""A made-up game for the stint builder's tests (#97): one regulation period, two teams, and only
the columns features/stints.py reads.

Home skaters are players 1 to 7 and goalies 30 and 31; away skaters 11 to 15 and goalie 40. By
default five skaters and a goalie of each team play the whole period, which makes one stint.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime

import polars as pl

GAME = 2019020001
SEASON = 20192020
GAME_DATE = date(2019, 10, 2)
OBSERVED = datetime(2019, 10, 3, 10, tzinfo=UTC)
HOME, AWAY = "TOR", "MTL"
HOME_SKATERS = (1, 2, 3, 4, 5)
AWAY_SKATERS = (11, 12, 13, 14, 15)
HOME_GOALIES = (30, 31)
AWAY_GOALIE = 40
PERIOD_END = 1200
XG_VERSION = "xg-20261001-abc1234"
XG_CUTOFF = datetime(2019, 4, 7, 10, tzinfo=UTC)
# The season's first game starts its fold; its xG model must be fitted before it.
SEASON_START = datetime(2019, 10, 2, 23, tzinfo=UTC)

Shift = tuple[int, int, int]  # player, start_s, end_s


def full_period() -> list[Shift]:
    players = (*HOME_SKATERS, HOME_GOALIES[0], *AWAY_SKATERS, AWAY_GOALIE)
    return [(player, 0, PERIOD_END) for player in players]


def without(shifts: list[Shift], player: int) -> list[Shift]:
    return [s for s in shifts if s[0] != player]


def shifts_frame(shifts: list[Shift]) -> pl.DataFrame:
    rows = []
    numbers: dict[int, int] = {}
    for player, start, end in sorted(shifts, key=lambda s: (s[0], s[1])):
        numbers[player] = numbers.get(player, 0) + 1
        rows.append(
            {
                "game_id": GAME,
                "team": AWAY if player in (*range(11, 16), AWAY_GOALIE) else HOME,
                "player_id": player,
                "period": 1,
                "shift_number": numbers[player],
                "start_s": start,
                "end_s": end,
                "observed_utc": OBSERVED,
            }
        )
    return pl.DataFrame(
        rows, schema_overrides={"period": pl.Int8, "start_s": pl.Int32, "end_s": pl.Int32}
    )


def lineups_frame() -> pl.DataFrame:
    rows = [{"player_id": p, "team": HOME, "is_home": True, "role": "F"} for p in (1, 2, 3, 6, 7)]
    rows += [{"player_id": p, "team": HOME, "is_home": True, "role": "D"} for p in (4, 5)]
    rows += [{"player_id": g, "team": HOME, "is_home": True, "role": "G"} for g in HOME_GOALIES]
    rows += [{"player_id": p, "team": AWAY, "is_home": False, "role": "F"} for p in AWAY_SKATERS]
    rows += [{"player_id": AWAY_GOALIE, "team": AWAY, "is_home": False, "role": "G"}]
    return pl.DataFrame(rows).with_columns(
        game_id=pl.lit(GAME, pl.Int64), observed_utc=pl.lit(OBSERVED)
    )


def coverage_frame(complete: bool = True) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "game_id": [GAME],
            "season": [SEASON],
            "game_date": [GAME_DATE],
            "complete": [complete],
            "observed_utc": [OBSERVED],
        },
        schema_overrides={"season": pl.Int32},
    )


Shot = tuple[int, int, bool, bool]  # event_id, seconds, is_home, is_goal


def shots_frame(
    shots: list[Shot], penalty_shots: tuple[int, ...] = (), no_coordinates: tuple[int, ...] = ()
) -> pl.DataFrame:
    """Shots of the made-up game; those in no_coordinates have none, so the xG model would not
    score them."""
    return pl.DataFrame(
        [
            {
                "game_id": GAME,
                "season": SEASON,
                "event_id": event_id,
                "period": 1,
                "seconds": seconds,
                "is_home": is_home,
                "is_goal": is_goal,
                "is_penalty_shot": event_id in penalty_shots,
                "is_empty_net": False,
                "x": None if event_id in no_coordinates else 60,
                "y": None if event_id in no_coordinates else 0,
                "observed_utc": OBSERVED,
            }
            for event_id, seconds, is_home, is_goal in shots
        ],
        schema={
            "game_id": pl.Int64,
            "season": pl.Int32,
            "event_id": pl.Int32,
            "period": pl.Int8,
            "seconds": pl.Int32,
            "is_home": pl.Boolean,
            "is_goal": pl.Boolean,
            "is_penalty_shot": pl.Boolean,
            "is_empty_net": pl.Boolean,
            "x": pl.Int16,
            "y": pl.Int16,
            "observed_utc": pl.Datetime("us", "UTC"),
        },
    )


def calendar_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {"season": [SEASON], "start_utc": [SEASON_START]}, schema_overrides={"season": pl.Int32}
    )


def shot_xg_frame(xg: dict[int, float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "game_id": [GAME] * len(xg),
            "season": [SEASON] * len(xg),
            "event_id": list(xg),
            "xg": list(xg.values()),
            "train_cutoff": [XG_CUTOFF] * len(xg),
            "artifact_version": [XG_VERSION] * len(xg),
        },
        schema={
            "game_id": pl.Int64,
            "season": pl.Int32,
            "event_id": pl.Int32,
            "xg": pl.Float64,
            "train_cutoff": pl.Datetime("us", "UTC"),
            "artifact_version": pl.String,
        },
    )


def faceoffs_frame(faceoffs: Sequence[tuple[int, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "game_id": [GAME] * len(faceoffs),
            "season": [SEASON] * len(faceoffs),
            "sort_order": list(range(len(faceoffs))),
            "period": [1] * len(faceoffs),
            "seconds": [s for s, _ in faceoffs],
            "zone": [z for _, z in faceoffs],
            "observed_utc": [OBSERVED] * len(faceoffs),
        },
        schema={
            "game_id": pl.Int64,
            "season": pl.Int32,
            "sort_order": pl.Int32,
            "period": pl.Int8,
            "seconds": pl.Int32,
            "zone": pl.String,
            "observed_utc": pl.Datetime("us", "UTC"),
        },
    )


def build(
    shifts: list[Shift],
    shots: Sequence[Shot] = (),
    xg: dict[int, float] | None = None,
    faceoffs: Sequence[tuple[int, str]] = (),
    complete: bool = True,
    penalty_shots: tuple[int, ...] = (),
    no_coordinates: tuple[int, ...] = (),
) -> pl.DataFrame:
    from nhl_edge.features import stints

    return stints.build(
        coverage_frame(complete),
        shifts_frame(shifts),
        lineups_frame(),
        shots_frame(list(shots), penalty_shots, no_coordinates),
        shot_xg_frame(xg or {}),
        faceoffs_frame(faceoffs),
        calendar_frame(),
    )


def empty_shot_xg() -> pl.DataFrame:
    return shot_xg_frame({})


def real_calendar(game_ids: Sequence[int]) -> pl.DataFrame:
    """Each real fixture game's season and start, as the season calendar of its fold start."""
    from feed_fixtures import games_row, start_utc

    return pl.DataFrame(
        {
            "season": [games_row(g)["season"] for g in game_ids],
            "start_utc": [start_utc(g) for g in game_ids],
        },
        schema={"season": pl.Int32, "start_utc": pl.Datetime("us", "UTC")},
    )
