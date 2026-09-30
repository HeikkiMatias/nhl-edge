"""Shots: every unblocked shot attempt in a game's play-by-play (docs/plan.md section 4).

Shots on goal, missed shots and goals in periods 1 to 4 become rows; blocked shots are not
unblocked attempts, and shootout attempts are not play. Each play carries a situationCode of four
digits: away goalie in net (1 or 0), away skaters, home skaters, home goalie in net. A pulled goalie
shows as 6 skaters. Penalty shots are 1 skater against 0 with the defending goalie in.

The API gives raw rink coordinates, and before 2019-20 it does not say which end each team
attacks. The direction is inferred per team and period from where its offensive-zone shots are:
their median x is past the far blue line. Where it could be checked (2019-20 onward, against
homeTeamDefendingSide) the inference agreed in every team-period of a sample of 954.
"""

import json
import statistics
from typing import Any

import polars as pl

from nhl_edge.ingest.feeds import FeedGame, check_game_id, clock_s
from nhl_edge.lake.schemas import (
    OT_PERIOD,
    PERIOD_S,
    SHOT_EVENTS,
    STRENGTH_SOURCES,
    Shots,
    dtypes,
)

SHOOTOUT = "SO"
OFFENSIVE_ZONE = "O"

# Attack direction per (period, team id): +1 when the team shoots at the net at x = +89.
Directions = dict[tuple[int, int], int]


def unblocked_attempts(plays: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        play
        for play in plays
        if play["typeDescKey"] in SHOT_EVENTS and play["periodDescriptor"]["periodType"] != SHOOTOUT
    ]


def attack_directions(shots: list[dict[str, Any]]) -> Directions:
    """Which end each team attacks in each period, from the median x of its offensive-zone shots.
    A team with no such shot in a period attacks the end opposite its opponent."""
    xs: dict[tuple[int, int], list[int]] = {}
    for play in shots:
        details = play["details"]
        if details.get("zoneCode") == OFFENSIVE_ZONE and details.get("xCoord") is not None:
            key = (play["periodDescriptor"]["number"], details["eventOwnerTeamId"])
            xs.setdefault(key, []).append(details["xCoord"])
    directions = {
        key: 1 if median > 0 else -1
        for key, values in xs.items()
        if (median := statistics.median(values)) != 0
    }
    for play in shots:
        period, team = play["periodDescriptor"]["number"], play["details"]["eventOwnerTeamId"]
        if (period, team) in directions:
            continue
        opponents = [d for (p, t), d in directions.items() if p == period and t != team]
        if len(opponents) == 1:
            directions[(period, team)] = -opponents[0]
    return directions


def situation(code: str | None, is_home: bool) -> tuple[int, int, bool] | None:
    """Skaters for and against the shooting team and whether the defending goalie is in net, from
    a situationCode, or None when the code is missing or malformed."""
    if code is None or len(code) != 4 or not code.isdigit():
        return None
    away_goalie, away_skaters, home_skaters, home_goalie = (int(c) for c in code)
    if is_home:
        return home_skaters, away_skaters, away_goalie == 1
    return away_skaters, home_skaters, home_goalie == 1


def shot_row(
    play: dict[str, Any], game: FeedGame, directions: Directions, raw_key: str
) -> dict[str, Any]:
    details = play["details"]
    period = play["periodDescriptor"]["number"]
    team_id = details["eventOwnerTeamId"]
    team = game.team(team_id)
    if team is None:
        raise ValueError(f"shot {play['eventId']} of {game.game_id} is by team {team_id}")
    clock = clock_s(play["timeInPeriod"])
    if clock is None or not 1 <= period <= OT_PERIOD:
        raise ValueError(f"shot {play['eventId']} of {game.game_id} has no game time")
    is_home = team_id == game.home_id
    event_type = play["typeDescKey"]
    shooter = details.get("scoringPlayerId" if event_type == "goal" else "shootingPlayerId")
    goalie_id = details.get("goalieInNetId")
    strength = situation(play.get("situationCode"), is_home)
    direction = directions.get((period, team_id))
    x, y = details.get("xCoord"), details.get("yCoord")
    return {
        **game.keys(),
        "event_id": play["eventId"],
        "sort_order": play["sortOrder"],
        "period": period,
        "seconds": (period - 1) * PERIOD_S + clock,
        "team": team,
        "is_home": is_home,
        "event_type": event_type,
        "is_goal": event_type == "goal",
        "shooter_id": shooter,
        "goalie_id": goalie_id,
        "shot_type": details.get("shotType"),
        "zone": details.get("zoneCode"),
        "x": x * direction if x is not None and direction else None,
        "y": y * direction if y is not None and direction else None,
        "skaters_for": strength[0] if strength else None,
        "skaters_against": strength[1] if strength else None,
        "strength": f"{strength[0]}v{strength[1]}" if strength else None,
        "is_empty_net": not strength[2] if strength else goalie_id is None,
        "is_penalty_shot": strength is not None and strength[:2] == (1, 0),
        "situation_code": play.get("situationCode") or None,
        "strength_source": STRENGTH_SOURCES[0] if strength else None,
        "observed_utc": game.observed_utc,
        "raw_key": raw_key,
    }


def parse_shots(body: bytes, game: FeedGame, raw_key: str) -> pl.DataFrame:
    """The game's unblocked shot attempts, validated against Shots."""
    data = json.loads(body)
    check_game_id(data, game.game_id, "play-by-play")
    shots = unblocked_attempts(data["plays"])
    directions = attack_directions(shots)
    rows = [shot_row(play, game, directions, raw_key) for play in shots]
    return Shots.validate(pl.DataFrame(rows, schema=dtypes(Shots)))
