"""Penalties and faceoffs from a game's play-by-play (#96, docs/plan.md section 4).

The player layer needs both (#12): each player's penalties taken and drawn give B3's expected
power plays, and the faceoff that opens a stint gives RAPM its zone start. Every play-by-play
cached since 2010-11 names the players: committedByPlayerId, drawnByPlayerId and
servedByPlayerId on a penalty, winningPlayerId and losingPlayerId on a faceoff. Shootout plays
are not play and are left out, as in Shots.

A faceoff's zoneCode is from the side of the team it is logged under, its winner, as for every
play but a blocked shot (ingest/shots.py). faceoffs keeps the home team's side.
"""

import json
from typing import Any

import polars as pl

from nhl_edge.ingest.feeds import FeedGame, check_game_id, clock_s
from nhl_edge.ingest.shots import OTHER_SIDE, SHOOTOUT
from nhl_edge.lake.schemas import OT_PERIOD, PERIOD_S, Faceoffs, Penalties, dtypes

PENALTY = "penalty"
FACEOFF = "faceoff"


def plays_of(data: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    """The plays of one type in periods 1 to 4."""
    return [
        play
        for play in data["plays"]
        if play["typeDescKey"] == kind and play["periodDescriptor"]["periodType"] != SHOOTOUT
    ]


def play_keys(play: dict[str, Any], game: FeedGame, raw_key: str) -> dict[str, Any]:
    """The columns penalties and faceoffs share, with the team the play is logged under."""
    details = play["details"]
    period = play["periodDescriptor"]["number"]
    clock = clock_s(play.get("timeInPeriod"))
    if clock is None or not 1 <= period <= OT_PERIOD:
        raise ValueError(f"{play['typeDescKey']} {play['eventId']} of {game.game_id} has no time")
    team_id = details.get("eventOwnerTeamId")
    team = game.team(team_id) if team_id is not None else None
    if team is None:
        raise ValueError(
            f"{play['typeDescKey']} {play['eventId']} of {game.game_id} is by {team_id}"
        )
    return {
        **game.keys(),
        "event_id": play["eventId"],
        "sort_order": play["sortOrder"],
        "period": period,
        "seconds": (period - 1) * PERIOD_S + clock,
        "team": team,
        "is_home": team_id == game.home_id,
        "observed_utc": game.observed_utc,
        "raw_key": raw_key,
    }


def penalty_row(play: dict[str, Any], game: FeedGame, raw_key: str) -> dict[str, Any]:
    details = play["details"]
    return {
        **play_keys(play, game, raw_key),
        "committed_by": details.get("committedByPlayerId"),
        "drawn_by": details.get("drawnByPlayerId"),
        "served_by": details.get("servedByPlayerId"),
        "type_code": details.get("typeCode"),
        "desc_key": details.get("descKey"),
        "duration_min": details.get("duration"),
    }


def faceoff_row(play: dict[str, Any], game: FeedGame, raw_key: str) -> dict[str, Any]:
    details = play["details"]
    keys = play_keys(play, game, raw_key)
    team, home_won = keys.pop("team"), keys.pop("is_home")
    zone = details.get("zoneCode")
    return {
        **keys,
        "winning_team": team,
        "home_won": home_won,
        "winner_id": details.get("winningPlayerId"),
        "loser_id": details.get("losingPlayerId"),
        "zone": zone if home_won or zone is None else OTHER_SIDE[zone],
    }


def parse_penalties(body: bytes, game: FeedGame, raw_key: str) -> pl.DataFrame:
    """The game's penalties, validated against Penalties."""
    data = json.loads(body)
    check_game_id(data, game.game_id, "play-by-play")
    rows = [penalty_row(play, game, raw_key) for play in plays_of(data, PENALTY)]
    return Penalties.validate(pl.DataFrame(rows, schema=dtypes(Penalties)))


def parse_faceoffs(body: bytes, game: FeedGame, raw_key: str) -> pl.DataFrame:
    """The game's faceoffs, validated against Faceoffs."""
    data = json.loads(body)
    check_game_id(data, game.game_id, "play-by-play")
    rows = [faceoff_row(play, game, raw_key) for play in plays_of(data, FACEOFF)]
    return Faceoffs.validate(pl.DataFrame(rows, schema=dtypes(Faceoffs)))


def boxscore_pim(body: bytes) -> dict[str, int]:
    """Each team's penalty minutes in a boxscore, summed over its players, by triCode: the check
    on the penalties with a player that the audit runs (#96). A bench minor is the team's, not a
    player's, so it is not in this sum."""
    data = json.loads(body)
    stats = data["playerByGameStats"]
    return {
        data[side]["abbrev"]: sum(
            player.get("pim") or 0 for group in stats[side].values() for player in group
        )
        for side in ("homeTeam", "awayTeam")
    }
