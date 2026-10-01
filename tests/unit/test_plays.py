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
from pandera.errors import SchemaError

from nhl_edge.ingest.plays import boxscore_pim, parse_faceoffs, parse_penalties
from nhl_edge.lake.schemas import OT_PERIOD

OTHER_SIDE = {"O": "D", "N": "N", "D": "O"}


def full(game_id: int) -> tuple[pl.DataFrame, pl.DataFrame]:
    game = feed_game(game_id)
    body, key = feed("play-by-play", game_id), raw_key("play-by-play", game)
    return parse_penalties(body, game, key), parse_faceoffs(body, game, key)


def cut(game_id: int) -> tuple[pl.DataFrame, pl.DataFrame]:
    game = TRIMMED_GAMES[game_id]
    body, key = trimmed("play-by-play", game_id), raw_key("play-by-play", game)
    return parse_penalties(body, game, key), parse_faceoffs(body, game, key)


def row(frame: pl.DataFrame, event_id: int) -> dict[str, Any]:
    return frame.filter(pl.col("event_id") == event_id).row(0, named=True)


def test_penalties_name_who_took_and_who_drew_them() -> None:
    # MTL beat ARI 6-2: a fight, three minors each way and a penalty shot.
    penalties, _ = full(MTL_ARI)
    assert penalties.height == 7
    fight = penalties.filter(pl.col("seconds") == 12 * 60 + 21)
    assert sorted(fight["team"]) == ["ARI", "MTL"]
    assert (fight["type_code"] == "MAJ").all() and (fight["duration_min"] == 5).all()
    mtl = fight.filter(pl.col("team") == "MTL").row(0, named=True)
    assert mtl["is_home"] and (mtl["committed_by"], mtl["drawn_by"]) == (8482964, 8475178)
    shot = penalties.filter(pl.col("type_code") == "PS").row(0, named=True)
    assert (shot["team"], shot["duration_min"], shot["drawn_by"]) == ("ARI", 0, 8480018)
    assert shot["seconds"] == 1200 + 18 * 60 + 27
    assert penalties["served_by"].is_null().all()


def test_double_minor_and_overtime() -> None:
    penalties, _ = full(2010020008)
    double = penalties.filter(pl.col("desc_key") == "high-sticking-double-minor").row(0, named=True)
    assert (double["type_code"], double["duration_min"]) == ("MIN", 4)
    overtime = penalties.filter(pl.col("period") == OT_PERIOD).row(0, named=True)
    assert overtime["seconds"] == 3600 + 4 * 60 + 53


def test_shootout_penalties_are_left_out() -> None:
    # A game misconduct logged in TBL and NYI's shootout is not play.
    penalties, faceoffs = cut(2013021096)
    plays = json.loads(trimmed("play-by-play", 2013021096))["plays"]
    shootout = [p for p in plays if p["periodDescriptor"]["periodType"] == "SO"]
    assert [p["eventId"] for p in shootout] == [590]
    assert penalties.height == 6 and 590 not in penalties["event_id"]
    assert row(penalties, 588)["seconds"] == 3600 + 3 * 60 + 51
    assert (faceoffs["period"] == OT_PERIOD).sum() == 4


def test_bench_minor_served_by_a_player() -> None:
    penalties, _ = cut(2010020001)
    bench = row(penalties, 376)
    assert (bench["team"], bench["is_home"], bench["type_code"]) == ("TOR", True, "BEN")
    assert bench["committed_by"] is None and bench["drawn_by"] is None
    assert bench["served_by"] == 8470667
    # Delay of game, puck over the glass: nobody drew it.
    over_glass = row(penalties, 552)
    assert (over_glass["committed_by"], over_glass["drawn_by"]) == (8468463, None)


def test_penalty_naming_no_one_who_took_it() -> None:
    # NJD's head coach got a game misconduct at NYR: nobody took it or served it.
    penalties, _ = cut(2012020671)
    coach = row(penalties, 675)
    assert (coach["team"], coach["is_home"], coach["type_code"]) == ("NJD", False, "GAM")
    assert coach["duration_min"] == 0
    assert coach["committed_by"] is None and coach["served_by"] is None
    assert coach["drawn_by"] == 8470192
    # Abuse of officials, a bench minor served by a player.
    bench = row(penalties, 201)
    assert (bench["type_code"], bench["committed_by"], bench["served_by"]) == ("BEN", None, 8471851)


@pytest.mark.parametrize("game_id", [2010020001, 2012020671, 2013021096])
def test_faceoff_zone_from_the_home_side(game_id: int) -> None:
    game = TRIMMED_GAMES[game_id]
    _, faceoffs = cut(game_id)
    plays = json.loads(trimmed("play-by-play", game_id))["plays"]
    logged = {p["eventId"]: p["details"] for p in plays if p["typeDescKey"] == "faceoff"}
    assert faceoffs.height == len(logged)
    # Each fixture has a faceoff won by each team in each zone.
    assert faceoffs.select("home_won", "zone").unique().height == 6
    for face in faceoffs.iter_rows(named=True):
        details = logged[face["event_id"]]
        home = details["eventOwnerTeamId"] == game.home_id
        assert face["home_won"] == home
        assert face["winning_team"] == (game.home if home else game.away)
        expected = details["zoneCode"] if home else OTHER_SIDE[details["zoneCode"]]
        assert face["zone"] == expected
        assert (face["winner_id"], face["loser_id"]) == (
            details["winningPlayerId"],
            details["losingPlayerId"],
        )


@pytest.mark.parametrize("game_id", OPENING_WEEK_GAMES)
def test_every_faceoff_in_play(game_id: int) -> None:
    _, faceoffs = full(game_id)
    plays = json.loads(feed("play-by-play", game_id))["plays"]
    in_play = [
        p
        for p in plays
        if p["typeDescKey"] == "faceoff" and p["periodDescriptor"]["periodType"] != "SO"
    ]
    assert faceoffs.height == len(in_play) > 0
    assert faceoffs["event_id"].is_unique().all()


def _edited(game_id: int, edit: dict[str, Any]) -> bytes:
    data = json.loads(trimmed("play-by-play", game_id))
    first = next(p for p in data["plays"] if p["typeDescKey"] == "penalty")
    first["details"].update(edit)
    return json.dumps(data).encode()


def test_a_penalty_of_another_team_fails() -> None:
    game = TRIMMED_GAMES[2010020001]
    with pytest.raises(ValueError, match="is by 99"):
        parse_penalties(_edited(2010020001, {"eventOwnerTeamId": 99}), game, "key")


def test_an_unknown_penalty_type_fails() -> None:
    # A new type code is schema drift, which should stop the ingest rather than pass unread.
    game = TRIMMED_GAMES[2010020001]
    with pytest.raises(SchemaError):
        parse_penalties(_edited(2010020001, {"typeCode": "NEW"}), game, "key")


def test_boxscore_penalty_minutes_match_the_penalties_with_a_player() -> None:
    penalties, _ = full(2010020003)
    feed_pim = (
        penalties.filter(pl.col("committed_by").is_not_null())
        .group_by("team")
        .agg(pl.col("duration_min").cast(pl.Int64).sum())
    )
    box = boxscore_pim(feed("boxscore", 2010020003))
    assert dict(feed_pim.iter_rows()) == box
    assert sum(box.values()) == 20
