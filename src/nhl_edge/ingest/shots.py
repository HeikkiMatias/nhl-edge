"""Shots: every unblocked shot attempt in a game's play-by-play (docs/plan.md section 4).

Shots on goal, missed shots and goals in periods 1 to 4 become rows; blocked shots are not
unblocked attempts, and shootout attempts are not play. Each play carries a situationCode of four
digits: away goalie in net (1 or 0), away skaters, home skaters, home goalie in net. A pulled goalie
shows as 6 skaters. Penalty shots are 1 skater against 0 with the defending goalie in.

The API gives raw rink coordinates, and before 2019-20 it does not say which end each team
attacks. The direction is inferred per team and period from where its offensive-zone shots are:
their median x is past the far blue line. Where it could be checked (2019-20 onward, against
homeTeamDefendingSide) the inference agreed in every team-period of a sample of 954.

Each shot also carries the play logged just before it in the same period, for the xG model's
rebound and rush flags (#73, ADR 0010). Plays are ordered by game time, then play order, since old
feeds log a few out of order. The play's zone, from the shooting team's side, comes from its x and
the shooting team's attack direction: past the far blue line (x = 25 once turned) is O. Without
them it comes from zoneCode, which is from the side of the team the play is logged under, except
for a blocked shot: that is logged under the team that took it, and its zoneCode is nearly always
from the blocking team's side. On a sample of 160 games, coordinates and zoneCode agreed for every
other kind of play, but for about 1% of hits (ADR 0010).
"""

import json
import statistics
from itertools import pairwise
from typing import Any

import polars as pl

from nhl_edge.ingest.feeds import FeedGame, check_game_id, clock_s
from nhl_edge.lake.schemas import (
    OT_PERIOD,
    PERIOD_S,
    SHOT_EVENTS,
    STRENGTH_SOURCES,
    ZONES,
    Shots,
    dtypes,
)

SHOOTOUT = "SO"
OFFENSIVE_ZONE = "O"
BLOCKED_SHOT = "blocked-shot"
OTHER_SIDE = {"O": "D", "N": "N", "D": "O"}
BLUE_LINE_X = 25

# The play before a shot, and the seconds from it to the shot.
Previous = tuple[dict[str, Any], int]

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


def previous_plays(plays: list[dict[str, Any]]) -> dict[int, Previous]:
    """For each play of periods 1 to 4 but a period's first, by event id: the play logged just
    before it in the same period, by game time and then play order, and the seconds between."""
    timed = []
    for play in plays:
        descriptor = play["periodDescriptor"]
        clock = clock_s(play.get("timeInPeriod"))
        if descriptor.get("periodType") != SHOOTOUT and clock is not None:
            timed.append((descriptor["number"], clock, play["sortOrder"], play))
    timed.sort(key=lambda t: t[:3])
    return {
        play["eventId"]: (before[3], clock - before[1])
        for before, (period, clock, _, play) in pairwise(timed)
        if before[0] == period
    }


def previous_fields(
    previous: Previous | None, team_id: int, period: int, directions: Directions
) -> dict[str, Any]:
    """The prev_ columns of a shot by team_id, from the play before it."""
    if previous is None:
        return dict.fromkeys(
            ("prev_event_type", "prev_seconds", "prev_by_shooting_team", "prev_zone")
        )
    play, seconds = previous
    details = play.get("details") or {}
    owner, x = details.get("eventOwnerTeamId"), details.get("xCoord")
    direction = directions.get((period, team_id))
    zone = None
    if x is not None and direction:
        along = x * direction
        zone = "O" if along > BLUE_LINE_X else "D" if along < -BLUE_LINE_X else "N"
    elif owner is not None and details.get("zoneCode") in ZONES:
        zone = details["zoneCode"]
        # From the logged team's side: a blocked shot's zoneCode is nearly always the blocker's.
        if play["typeDescKey"] == BLOCKED_SHOT:
            zone = OTHER_SIDE[zone]
        if owner != team_id:
            zone = OTHER_SIDE[zone]
    return {
        "prev_event_type": play["typeDescKey"],
        "prev_seconds": seconds,
        "prev_by_shooting_team": None if owner is None else owner == team_id,
        "prev_zone": zone,
    }


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
    play: dict[str, Any],
    game: FeedGame,
    directions: Directions,
    previous: Previous | None,
    raw_key: str,
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
        **previous_fields(previous, team_id, period, directions),
        "observed_utc": game.observed_utc,
        "raw_key": raw_key,
    }


def parse_shots(body: bytes, game: FeedGame, raw_key: str) -> pl.DataFrame:
    """The game's unblocked shot attempts, validated against Shots."""
    data = json.loads(body)
    check_game_id(data, game.game_id, "play-by-play")
    shots = unblocked_attempts(data["plays"])
    directions = attack_directions(shots)
    previous = previous_plays(data["plays"])
    rows = [
        shot_row(play, game, directions, previous.get(play["eventId"]), raw_key) for play in shots
    ]
    return Shots.validate(pl.DataFrame(rows, schema=dtypes(Shots)))
