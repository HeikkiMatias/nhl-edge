"""The blocked shot attempts of a game's play-by-play (#419): who took and blocked each, where,
at what strength, and their count against the boxscore's blocks, on the full fixture feeds."""

import json
from collections import Counter
from typing import Any

import polars as pl
import pytest
from feed_fixtures import MTL_ARI, OPENING_WEEK_GAMES, feed, feed_game, parsed_feeds, raw_key

from nhl_edge.ingest.shots import blocked_zone, parse_blocked_shots, parse_shots

GAMES = (*OPENING_WEEK_GAMES, MTL_ARI)


def blocked_of(game_id: int, body: bytes | None = None) -> pl.DataFrame:
    game = feed_game(game_id)
    body = feed("play-by-play", game_id) if body is None else body
    return parse_blocked_shots(body, game, raw_key("play-by-play", game))


def plays_of(game_id: int) -> list[dict[str, Any]]:
    return json.loads(feed("play-by-play", game_id))["plays"]


def rosters(game_id: int) -> tuple[dict[int, str], Counter[str]]:
    """Each dressed player's team, and each team's blocks, from the boxscore."""
    box = json.loads(feed("boxscore", game_id))
    team_of: dict[int, str] = {}
    blocks: Counter[str] = Counter()
    for side in ("awayTeam", "homeTeam"):
        code = box[side]["abbrev"]
        for group in ("forwards", "defense", "goalies"):
            for player in box["playerByGameStats"][side].get(group, []):
                team_of[player["playerId"]] = code
                blocks[code] += player.get("blockedShots") or 0
    return team_of, blocks


@pytest.mark.parametrize("game_id", GAMES)
def test_each_teams_blocked_attempts_are_the_other_teams_blocks(game_id: int) -> None:
    blocked = blocked_of(game_id)
    team_of, blocks = rosters(game_id)
    game = feed_game(game_id)
    taken = Counter(blocked["team"].to_list())
    assert taken[game.home] == blocks[game.away] and taken[game.away] == blocks[game.home]
    # The shooter is on the team the attempt is logged under, the blocker on the other.
    for row in blocked.iter_rows(named=True):
        assert team_of[row["shooter_id"]] == row["team"]
        assert team_of[row["blocker_id"]] != row["team"]


def test_blocked_attempts_in_play_only() -> None:
    # CAR at MIN went to a shootout and overtime; only the blocked attempts in play are rows.
    plays = plays_of(2010020008)
    in_play = [
        p
        for p in plays
        if p["typeDescKey"] == "blocked-shot" and p["periodDescriptor"]["periodType"] != "SO"
    ]
    blocked = blocked_of(2010020008)
    assert blocked.height == len(in_play) == 27
    assert set(blocked["event_id"]) == {p["eventId"] for p in in_play}
    # None is also an unblocked attempt.
    assert not set(blocked["event_id"]) & set(parse_shots_ids(2010020008))


def parse_shots_ids(game_id: int) -> list[int]:
    game = feed_game(game_id)
    body = feed("play-by-play", game_id)
    return parse_shots(body, game, raw_key("play-by-play", game))["event_id"].to_list()


@pytest.mark.parametrize("game_id", GAMES)
def test_coordinates_face_the_net_the_shooting_team_attacks(game_id: int) -> None:
    blocked = blocked_of(game_id)
    plays = {p["eventId"]: p for p in plays_of(game_id)}
    assert blocked["x"].null_count() == 0
    # Attempts are blocked on the way to the net the shooting team attacks: its offensive zone.
    assert (blocked["x"] > 25).all() and (blocked["zone"] == "O").all()
    # The logged zoneCode is the blocking team's side: D for every one of them.
    assert {plays[e]["details"]["zoneCode"] for e in blocked["event_id"]} == {"D"}
    # The turn is the one the game's unblocked shots give the same team and period.
    shots = parse_shots(
        feed("play-by-play", game_id),
        feed_game(game_id),
        raw_key("play-by-play", feed_game(game_id)),
    )
    sign = {
        (r["period"], r["team"]): (
            1 if r["x"] * plays[r["event_id"]]["details"]["xCoord"] > 0 else -1
        )
        for r in shots.filter(pl.col("x") != 0).iter_rows(named=True)
    }
    for row in blocked.iter_rows(named=True):
        raw = plays[row["event_id"]]["details"]
        assert row["x"] == sign[(row["period"], row["team"])] * raw["xCoord"]


def test_without_coordinates_the_zone_is_the_logged_one_turned_around() -> None:
    assert blocked_zone(None, "D") == "O" and blocked_zone(None, "O") == "D"
    assert blocked_zone(None, "N") == "N" and blocked_zone(None, None) is None
    assert blocked_zone(70, "O") == "O" and blocked_zone(0, "D") == "N"
    assert blocked_zone(-30, None) == "D"


def test_strength_is_the_shooting_teams_view() -> None:
    blocked = blocked_of(MTL_ARI)
    plays = {p["eventId"]: p for p in plays_of(MTL_ARI)}
    for row in blocked.iter_rows(named=True):
        code = plays[row["event_id"]]["situationCode"]
        away, home = int(code[1]), int(code[2])
        own, other = (home, away) if row["is_home"] else (away, home)
        assert (row["skaters_for"], row["skaters_against"]) == (own, other)
        assert row["strength"] == f"{own}v{other}" and row["strength_source"] == "situation_code"


def test_a_complete_chart_gives_the_skater_counts() -> None:
    parsed = parsed_feeds(MTL_ARI)
    assert parsed["shift_coverage"]["complete"].item()
    blocked = parsed["blocked_shots"]
    assert blocked.height == 29 and (blocked["strength_source"] == "chart").all()


def test_missing_situation_code_leaves_strength_and_the_empty_net_unknown() -> None:
    data = json.loads(feed("play-by-play", MTL_ARI))
    first = next(p for p in data["plays"] if p["typeDescKey"] == "blocked-shot")
    del first["situationCode"]
    blocked = blocked_of(MTL_ARI, json.dumps(data).encode())
    row = blocked.filter(pl.col("event_id") == first["eventId"]).row(0, named=True)
    assert row["situation_code"] is None and row["strength"] is None
    assert row["skaters_for"] is None and row["strength_source"] is None
    assert row["is_empty_net"] is None
    assert blocked["is_empty_net"].null_count() == 1


def test_a_feed_of_another_game_is_rejected() -> None:
    game = feed_game(MTL_ARI)
    with pytest.raises(ValueError, match="play-by-play"):
        parse_blocked_shots(feed("play-by-play", OPENING_WEEK_GAMES[0]), game, "k")


def test_the_unblocked_shots_are_unchanged() -> None:
    # parse_shots still leaves every blocked attempt out.
    shots = parsed_feeds(MTL_ARI)["shots"]
    assert (
        shots.height
        == parse_shots(
            feed("play-by-play", MTL_ARI),
            feed_game(MTL_ARI),
            raw_key("play-by-play", feed_game(MTL_ARI)),
        ).height
    )
    assert "blocked-shot" not in set(shots["event_type"])
