import json
from typing import Any

import polars as pl
import pytest
from feed_fixtures import (
    MTL_ARI,
    OPENING_WEEK_GAMES,
    TRIMMED_GAMES,
    feed,
    feed_game,
    raw_key,
    trimmed,
)

from nhl_edge.ingest.shots import attack_directions, parse_shots, situation
from nhl_edge.lake.schemas import SHOT_EVENTS


def shots_of(game_id: int) -> pl.DataFrame:
    game = feed_game(game_id)
    return parse_shots(feed("play-by-play", game_id), game, raw_key("play-by-play", game))


def plays_of(game_id: int) -> list[dict[str, Any]]:
    return json.loads(feed("play-by-play", game_id))["plays"]


MTL = shots_of(MTL_ARI)


def event(event_id: int) -> dict[str, Any]:
    return MTL.filter(pl.col("event_id") == event_id).row(0, named=True)


def test_unblocked_attempts_in_play_only() -> None:
    # CAR at MIN went to a shootout: its attempts and every blocked shot are left out.
    shots = shots_of(2010020008)
    plays = plays_of(2010020008)
    in_play = [
        p
        for p in plays
        if p["typeDescKey"] in SHOT_EVENTS and p["periodDescriptor"]["periodType"] != "SO"
    ]
    assert any(p["periodDescriptor"]["periodType"] == "SO" for p in plays)
    assert shots.height == len(in_play) == 101
    assert shots["period"].max() == 4
    assert set(shots["event_type"]) == set(SHOT_EVENTS)
    assert (shots["is_goal"] == (shots["event_type"] == "goal")).all()


def test_seconds_are_elapsed_game_time() -> None:
    # Penalty-shot goal at 18:27 of the second period.
    assert event(466)["seconds"] == 1200 + 18 * 60 + 27
    overtime = shots_of(2010020008).filter(pl.col("period") == 4)
    assert overtime["seconds"].is_between(3600, 3900).all()


def test_strength_from_the_shooting_team() -> None:
    # 0651: ARI's goalie pulled for a sixth skater, so MTL shoots 5 against 6 at an empty net.
    empty_net_goal = event(856)
    assert (empty_net_goal["team"], empty_net_goal["situation_code"]) == ("MTL", "0651")
    assert (empty_net_goal["skaters_for"], empty_net_goal["skaters_against"]) == (5, 6)
    assert empty_net_goal["strength"] == "5v6"
    assert empty_net_goal["is_empty_net"] is True
    assert empty_net_goal["goalie_id"] is None
    even = MTL.filter(pl.col("situation_code") == "1551")
    assert set(even["strength"]) == {"5v5"}
    assert not even["is_empty_net"].any()


def test_empty_net_follows_the_defending_goalie() -> None:
    for row in MTL.iter_rows(named=True):
        code = row["situation_code"]
        defending_goalie = code[0] if row["is_home"] else code[3]
        assert row["is_empty_net"] == (defending_goalie == "0")
    assert MTL.filter("is_empty_net")["event_id"].to_list() == [854, 856]


def test_penalty_shot_is_flagged() -> None:
    penalty_shot = event(466)
    assert (penalty_shot["situation_code"], penalty_shot["strength"]) == ("1010", "1v0")
    assert penalty_shot["is_penalty_shot"] is True
    assert penalty_shot["is_empty_net"] is False
    assert MTL["is_penalty_shot"].sum() == 1


def test_coordinates_face_the_attacked_net() -> None:
    # From 2019-20 on, play-by-play says which end the home team defends: the inferred direction
    # must agree with it for every shot.
    game = feed_game(MTL_ARI)
    raw = {p["eventId"]: p for p in plays_of(MTL_ARI) if p["typeDescKey"] in SHOT_EVENTS}
    for row in MTL.iter_rows(named=True):
        play = raw[row["event_id"]]
        attacks_right = (play["homeTeamDefendingSide"] == "left") == (
            play["details"]["eventOwnerTeamId"] == game.home_id
        )
        sign = 1 if attacks_right else -1
        assert (row["x"], row["y"]) == (
            sign * play["details"]["xCoord"],
            sign * play["details"]["yCoord"],
        )
    # The empty-net goal went in from MTL's own zone.
    assert (event(856)["zone"], event(856)["x"]) == ("D", -43)


@pytest.mark.parametrize("game_id", OPENING_WEEK_GAMES)
def test_old_seasons_without_a_defending_side(game_id: int) -> None:
    assert not any("homeTeamDefendingSide" in p for p in plays_of(game_id))
    shots = shots_of(game_id)
    assert shots["x"].null_count() == 0
    assert (shots.filter(pl.col("zone") == "O")["x"] > 25).all()
    # Zone codes of old plays sometimes contradict their coordinates (in 2010020008 a shot coded D
    # is at x = 78), which is why the direction comes from the median of many shots.


def test_missing_situation_code_leaves_strength_unknown() -> None:
    game = TRIMMED_GAMES[2010020124]
    shots = parse_shots(trimmed("play-by-play", 2010020124), game, "k")
    unknown = shots.filter(pl.col("situation_code").is_null())
    assert unknown.height == 21
    assert unknown.select("skaters_for", "skaters_against", "strength").null_count().row(0) == (
        21,
        21,
        21,
    )
    assert not unknown["is_penalty_shot"].any()
    # Without a code, the empty-net flag falls back to the goalie in net.
    assert (unknown["is_empty_net"] == unknown["goalie_id"].is_null()).all()


def test_situation_code() -> None:
    assert situation("1551", is_home=True) == (5, 5, True)
    assert situation("1560", is_home=False) == (5, 6, False)  # home goalie pulled
    assert situation("0101", is_home=False) == (1, 0, True)  # away penalty shot
    for bad in (None, "", "15", "15a1"):
        assert situation(bad, is_home=True) is None


def play(period: int, team: int, x: int, zone: str | None = "O") -> dict[str, Any]:
    return {
        "periodDescriptor": {"number": period},
        "details": {"eventOwnerTeamId": team, "xCoord": x, "zoneCode": zone},
    }


def test_attack_direction_falls_back_to_the_opponent() -> None:
    directions = attack_directions(
        [
            play(1, 6, 70),
            play(1, 6, 80),
            play(1, 6, -60),  # a shot from the other end does not flip the median
            play(1, 25, -30, zone="N"),  # team 25 has no offensive-zone shot in period 1
            play(2, 6, 10, zone=None),  # nothing to go on in period 2
        ]
    )
    assert directions == {(1, 6): 1, (1, 25): -1}


def test_a_feed_of_another_game_is_rejected() -> None:
    with pytest.raises(ValueError, match="is for game 2010020004"):
        parse_shots(feed("play-by-play", 2010020004), feed_game(2010020003), "k")
