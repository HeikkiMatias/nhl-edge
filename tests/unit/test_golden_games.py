"""Golden games (docs/plan.md section 11): the cases parsers and settlement logic get wrong, on five
real games frozen in tests/golden/. The moneyline settles on the full game, overtime and shootout
included (hard rule 2), so a tied regulation never settles it."""

import hashlib
import json
from typing import Any

import polars as pl
import pytest
from golden_games import CASES, GOLDEN_DIR, Bodies, drift, frozen_body, parse_case

from nhl_edge.ingest.feeds import FeedGame
from nhl_edge.ingest.shift_coverage import on_ice_counts

TABLES = {name: parse_case(case) for name, case in CASES.items()}


def game(case: str) -> dict[str, Any]:
    return TABLES[case]["games"].row(0, named=True)


def goal_at(case: str, seconds: int) -> dict[str, Any]:
    shots = TABLES[case]["shots"]
    return shots.filter(pl.col("is_goal") & (pl.col("seconds") == seconds)).row(0, named=True)


def goals_by_team(case: str, *, last_period: int = 4) -> dict[str, int]:
    shots = TABLES[case]["shots"].filter(pl.col("is_goal") & (pl.col("period") <= last_period))
    counts = dict(shots.group_by("team").len().iter_rows())
    return {team: counts.get(team, 0) for team in (game(case)["away"], game(case)["home"])}


def test_the_manifest_matches_the_frozen_files() -> None:
    assert set(CASES) == {
        "regulation",
        "overtime",
        "shootout",
        "late_empty_net",
        "five_on_three",
    }
    for case in CASES.values():
        for path, sha256 in case["files"].items():
            assert hashlib.sha256((GOLDEN_DIR / path).read_bytes()).hexdigest() == sha256, path


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("regulation", ("SEA", "VGK", 1, 4, "REG")),
        ("overtime", ("TBL", "BUF", 2, 3, "OT")),
        ("shootout", ("CAR", "LAK", 6, 5, "SO")),
        ("late_empty_net", ("TOR", "FLA", 1, 3, "REG")),
        ("five_on_three", ("PHI", "OTT", 2, 5, "REG")),
    ],
)
def test_final_score_and_how_the_game_was_decided(case: str, expected: tuple[Any, ...]) -> None:
    row = game(case)
    assert (
        row["away"],
        row["home"],
        row["away_score"],
        row["home_score"],
        row["decided_in"],
    ) == expected


@pytest.mark.parametrize(
    ("case", "regulation", "winner"), [("overtime", 2, "BUF"), ("shootout", 5, "CAR")]
)
def test_the_moneyline_settles_on_the_full_game_not_regulation(
    case: str, regulation: int, winner: str
) -> None:
    # Regulation ended tied: a 60-minute (3-way) line would settle as a draw. The moneyline goes
    # to the full-game winner, whom the games table's scores name.
    assert set(goals_by_team(case, last_period=3).values()) == {regulation}
    row = game(case)
    full_game_winner = row["home"] if row["home_score"] > row["away_score"] else row["away"]
    assert full_game_winner == winner
    assert abs(int(row["home_score"]) - int(row["away_score"])) == 1


@pytest.mark.parametrize("case", sorted(CASES))
def test_goals_add_up_to_the_final_score(case: str) -> None:
    # A shootout credits its winner with one goal that is not a shot.
    row = game(case)
    goals = goals_by_team(case)
    shootout_goal = int(row["decided_in"] == "SO")
    away_won = row["away_score"] > row["home_score"]
    assert goals[row["away"]] + shootout_goal * away_won == row["away_score"]
    assert goals[row["home"]] + shootout_goal * (not away_won) == row["home_score"]


def test_shootout_attempts_are_not_shots() -> None:
    plays = json.loads(frozen_body(f"play-by-play_{CASES['shootout']['game_id']}"))["plays"]
    assert any(p["periodDescriptor"]["periodType"] == "SO" for p in plays)
    shots = TABLES["shootout"]["shots"]
    assert shots["period"].max() == 4
    assert goals_by_team("shootout") == {"CAR": 5, "LAK": 5}


def test_late_empty_net_goal() -> None:
    # 59:59, TOR's goalie pulled for a sixth skater while FLA were a man short.
    goal = goal_at("late_empty_net", 3599)
    assert goal["team"] == "FLA"
    assert (goal["situation_code"], goal["strength"]) == ("0641", "4v6")
    assert goal["is_empty_net"] is True
    assert goal["goalie_id"] is None
    before = TABLES["late_empty_net"]["shots"].filter(
        pl.col("is_goal") & (pl.col("seconds") < 3599)
    )
    assert dict(before.group_by("team").len().iter_rows()) == {"FLA": 2, "TOR": 1}


@pytest.mark.parametrize(
    ("case", "seconds", "team", "strength", "empty_net"),
    [
        ("regulation", 3525, "VGK", "5v6", True),  # into SEA's empty net at 58:45
        ("overtime", 3593, "TBL", "6v5", False),  # TBL's extra attacker ties it at 59:53
        ("shootout", 3518, "LAK", "6v5", False),  # LAK's extra attacker ties it at 58:38
    ],
)
def test_empty_net_is_the_defending_goalie_pulled_not_the_shooting_one(
    case: str, seconds: int, team: str, strength: str, empty_net: bool
) -> None:
    goal = goal_at(case, seconds)
    assert (goal["team"], goal["strength"], goal["is_empty_net"]) == (team, strength, empty_net)
    assert (goal["goalie_id"] is None) == empty_net


def test_overtime_winner_at_three_on_three() -> None:
    goal = goal_at("overtime", 3706)  # 1:46 of overtime
    assert (goal["period"], goal["team"], goal["strength"]) == (4, "BUF", "3v3")


def test_five_on_three_goal_with_the_players_on_the_ice() -> None:
    goal = goal_at("five_on_three", 1011)  # 16:51 of the first period
    assert (goal["team"], goal["situation_code"], goal["strength"]) == ("PHI", "1531", "5v3")
    tables = TABLES["five_on_three"]
    boxscore = frozen_body(f"boxscore_{CASES['five_on_three']['game_id']}")
    feed_game = FeedGame.from_boxscore(game("five_on_three"), boxscore)
    counts = on_ice_counts(
        feed_game, tables["shots"], tables["shifts"], tables["actual_lineups"]
    ).filter(pl.col("event_id") == goal["event_id"])
    # PHI are away: 5 skaters and a goalie against OTT's 3 and a goalie.
    assert counts.select(
        "away_skaters_on", "away_goalie_on", "home_skaters_on", "home_goalie_on"
    ).row(0) == (5, 1, 3, 1)


@pytest.mark.parametrize("case", sorted(CASES))
def test_charts_are_complete_and_lineups_whole(case: str) -> None:
    coverage = TABLES[case]["shift_coverage"].row(0, named=True)
    assert coverage["complete"] is True
    assert (coverage["skater_mismatches"], coverage["goalie_mismatches"]) == (0, 0)
    lineups = TABLES[case]["actual_lineups"]
    assert lineups.height == 40
    assert dict(lineups.filter("starting_goalie").group_by("team").len().iter_rows()) == {
        game(case)["away"]: 1,
        game(case)["home"]: 1,
    }


# The nightly contract test compares live responses with the frozen ones through drift(). These
# check that it notices a change the parsers depend on and ignores one they don't.
LATE = CASES["late_empty_net"]


def edited(prefix: str, old: bytes, new: bytes) -> Bodies:
    """The frozen responses, with one edit in those whose name starts with prefix."""

    def bodies(name: str) -> bytes:
        body = frozen_body(name)
        return body.replace(old, new) if name.startswith(prefix) else body

    return bodies


def test_identical_responses_are_no_drift() -> None:
    assert drift(LATE, frozen_body) == []


def test_a_renamed_key_the_parsers_read_is_drift() -> None:
    problems = drift(LATE, edited("play-by-play", b'"situationCode"', b'"situation"'))
    assert "shots: parsed rows differ from the frozen copy" in problems
    assert any("keys gone: " in p and "plays[].situationCode" in p for p in problems)
    assert any("keys new: " in p and "plays[].situation" in p for p in problems)


def test_a_new_key_the_parsers_ignore_is_no_drift() -> None:
    added = edited("boxscore", b'"gameType"', b'"broadcastNote": "x", "gameType"')
    assert drift(LATE, added) == []


def test_a_response_that_no_longer_parses_is_drift() -> None:
    problems = drift(LATE, edited("boxscore", b'"playerByGameStats"', b'"playerStats"'))
    assert problems[0].startswith("parsing the live responses failed")
