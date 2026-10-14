"""Policy v2's starters at the decision (ADR 0037, #222): the calls, the order of the sources, the
name match, a likely starter's chance, and the goalie rows B2, B3 and u then read."""

import polars as pl
import pytest
from confirmed_fixtures import (
    BOBROVSKY_EFFECT,
    CUTOFF,
    EXPECTED_SHOTS,
    GAME,
    TEAM_GAMES,
    XG_PER_SHOT,
    at,
    candidates,
    dailyfaceoff,
    goal_multipliers,
    goalie_effects,
    goalie_starts,
    goalies,
    nhl,
)

from nhl_edge.lake.schemas import GoalieEffects, GoalieStarts, GoalMultipliers
from nhl_edge.lineup import confirmed

L = 0.85


def named(nhl_rows: list, dfo_rows: list, likely_share: float = L) -> pl.DataFrame:
    called = confirmed.calls(TEAM_GAMES, nhl(nhl_rows), dailyfaceoff(dfo_rows))
    return confirmed.starters(called, candidates(), goalies(), likely_share)


def row(frame: pl.DataFrame, team: str) -> dict[str, object]:
    return frame.filter(pl.col("team") == team).row(0, named=True)


def starts_of(frame: pl.DataFrame, team: str) -> dict[int, float]:
    rows = frame.filter(pl.col("team") == team).sort("goalie_id")
    return dict(zip(rows["goalie_id"].to_list(), rows["p_start"].to_list(), strict=True))


def test_each_source_is_read_at_its_latest_poll_before_the_moment() -> None:
    called = confirmed.calls(
        TEAM_GAMES,
        nhl([("MTL", at(15, 50), 10), ("MTL", at(16, 30), None)]),
        dailyfaceoff(
            [
                ("TOR", at(13, 50), "Joseph Woll", "Likely"),
                ("TOR", at(16, 30), "Anthony Stolarz", "Confirmed"),
            ]
        ),
    )
    # The NHL's flag was withdrawn by its latest poll; Daily Faceoff changed its pick.
    assert row(called, "MTL")["nhl_id"] is None
    assert (row(called, "TOR")["dfo_name"], row(called, "TOR")["dfo_status"]) == (
        "Anthony Stolarz",
        "Confirmed",
    )


def test_a_team_listed_twice_reads_the_listing_nearest_its_start() -> None:
    from datetime import timedelta

    from confirmed_fixtures import START

    stale = dailyfaceoff(
        [("TOR", at(16, 30), "Joseph Woll", "Confirmed")], START - timedelta(hours=3)
    )
    current = dailyfaceoff([("TOR", at(15, 50), "Anthony Stolarz", "Likely")])
    called = confirmed.calls(TEAM_GAMES, nhl([]), pl.concat([stale, current]))
    assert row(called, "TOR")["dfo_name"] == "Anthony Stolarz"


def test_the_nhl_flag_comes_first_then_confirmed_then_likely() -> None:
    starters = named(
        [("MTL", at(16, 0), 11)],
        [
            ("MTL", at(16, 30), "Sam Montembeault", "Confirmed"),
            ("TOR", at(16, 30), "Joseph Woll", "Likely"),
        ],
    )
    mtl, tor = row(starters, "MTL"), row(starters, "TOR")
    assert (mtl["source"], mtl["goalie_id"], mtl["p"]) == (confirmed.NHL, 11, 1.0)
    assert (tor["source"], tor["goalie_id"], tor["p"]) == (confirmed.LIKELY, 21, L)
    # Daily Faceoff's Confirmed is certain without a flag.
    alone = named([], [("MTL", at(16, 30), "Sam Montembeault", "Confirmed")])
    assert (row(alone, "MTL")["source"], row(alone, "MTL")["p"]) == (confirmed.CONFIRMED, 1.0)
    # No status names no starter.
    assert named([], [("MTL", at(16, 30), "Sam Montembeault", None)]).is_empty()


def test_a_likely_starter_is_never_less_likely_than_the_model_had_him() -> None:
    # The model already gave Montembeault 0.7: below L, so L; with L at 0.6, his 0.7 stands.
    likely = [("MTL", at(16, 30), "Sam Montembeault", "Likely")]
    assert row(named([], likely), "MTL")["p"] == L
    assert row(named([], likely, 0.6), "MTL")["p"] == 0.7
    with pytest.raises(ValueError, match="likely_share"):
        named([], likely, 0.0)


def test_daily_faceoffs_name_matches_the_teams_candidates_then_the_league() -> None:
    starters = named(
        [],
        [
            ("MTL", at(16, 30), "Jakub Dobes", "Confirmed"),  # no accent
            ("TOR", at(16, 30), "Sergei Bobrovsky", "Confirmed"),  # not a TOR candidate
        ],
    )
    assert row(starters, "MTL")["goalie_id"] == 11
    assert row(starters, "TOR")["goalie_id"] == 30
    unknown = named([], [("TOR", at(16, 30), "Someone New", "Confirmed")])
    assert row(unknown, "TOR")["goalie_id"] == confirmed.UNKNOWN_GOALIE


def test_a_certain_starter_takes_his_teams_whole_chance() -> None:
    starters = named([], [("MTL", at(16, 30), "Jakub Dobeš", "Confirmed")])
    starts, _, _ = confirmed.adjusted(
        goalie_starts(), goalie_effects(), goal_multipliers(), starters, CUTOFF
    )
    GoalieStarts.validate(starts)
    assert starts_of(starts, "MTL") == {11: 1.0}
    assert starts_of(starts, "TOR") == {20: 0.6, 21: 0.4}
    # The team's row is known from the call.
    assert row(starts, "MTL")["observed_utc"] == at(16, 30)
    # u's goalie doubt, 1 - the likeliest p_start: 0 for a confirmed starter.
    top = starts.group_by("team").agg(pl.col("p_start").max()).sort("team")
    assert (1 - top["p_start"]).to_list() == [0.0, pytest.approx(0.4)]


def test_a_likely_starter_leaves_the_rest_to_the_others_in_proportion() -> None:
    starters = named([], [("MTL", at(16, 30), "Jakub Dobeš", "Likely")])
    starts, _, _ = confirmed.adjusted(
        goalie_starts(), goalie_effects(), goal_multipliers(), starters, CUTOFF
    )
    assert starts_of(starts, "MTL") == {10: pytest.approx(1 - L), 11: L}
    # His team's goalie doubt is 1 - L: more than a confirmed starter's 0.
    assert 1 - max(starts_of(starts, "MTL").values()) == pytest.approx(1 - L)


def test_a_starter_outside_the_candidates_is_rated_from_his_latest_row() -> None:
    starters = named([], [("TOR", at(16, 30), "Sergei Bobrovsky", "Likely")])
    starts, effects, multipliers = confirmed.adjusted(
        goalie_starts(), goalie_effects(), goal_multipliers(), starters, CUTOFF
    )
    for model, frame in ((GoalieStarts, starts), (GoalieEffects, effects)):
        model.validate(frame)
    GoalMultipliers.validate(multipliers)
    # The others share 1 - L in the model's proportions, 0.6 to 0.4.
    assert starts_of(starts, "TOR") == {
        20: pytest.approx(0.6 * (1 - L)),
        21: pytest.approx(0.4 * (1 - L)),
        30: L,
    }
    his = effects.filter(pl.col("game_id") == GAME, pl.col("goalie_id") == 30).row(0, named=True)
    assert his["team"] == "TOR" and his["effect"] == BOBROVSKY_EFFECT
    assert his["goals_saved"] == pytest.approx(BOBROVSKY_EFFECT * EXPECTED_SHOTS["TOR"])
    # MTL's attack faces him at his gamma.
    attack = multipliers.filter(pl.col("goalie_id") == 30).row(0, named=True)
    assert (attack["team"], attack["opponent"]) == ("MTL", "TOR")
    assert attack["gamma"] == pytest.approx(1 - BOBROVSKY_EFFECT / XG_PER_SHOT)
    assert attack["multiplier"] == pytest.approx(1.02 * attack["gamma"])


def test_a_name_matching_no_goalie_starts_as_an_average_one() -> None:
    starters = named([], [("TOR", at(16, 30), "Someone New", "Confirmed")])
    starts, effects, multipliers = confirmed.adjusted(
        goalie_starts(), goalie_effects(), goal_multipliers(), starters, CUTOFF
    )
    assert starts_of(starts, "TOR") == {confirmed.UNKNOWN_GOALIE: 1.0}
    unknown = pl.col("goalie_id") == confirmed.UNKNOWN_GOALIE
    assert effects.filter(unknown)["effect"].to_list() == [0.0]
    assert multipliers.filter(unknown)["gamma"].to_list() == [1.0]


def test_no_call_leaves_the_goalie_rows_as_they_are() -> None:
    starters = named([], [])
    frames = confirmed.adjusted(
        goalie_starts(), goalie_effects(), goal_multipliers(), starters, CUTOFF
    )
    for got, want in zip(
        frames, (goalie_starts(), goalie_effects(), goal_multipliers()), strict=True
    ):
        assert got.equals(want)


def test_a_first_name_that_differs_matches_by_surname_on_the_team() -> None:
    # Daily Faceoff wrote Matthew Murray for the NHL's Matt Murray (#238): a surname unique
    # among the team's candidates is the goalie.
    starters = named([], [("MTL", at(16, 30), "Jake Dobes", "Confirmed")])
    assert row(starters, "MTL")["goalie_id"] == 11
    # Two candidates sharing the surname: no surname match, and the name matches no one else.
    twins = candidates().with_columns(
        name=pl.when(pl.col("goalie_id") == 10).then(pl.lit("Jan Dobeš")).otherwise("name")
    )
    called = confirmed.calls(
        TEAM_GAMES, nhl([]), dailyfaceoff([("MTL", at(16, 30), "Jake Dobes", "Confirmed")])
    )
    ambiguous = confirmed.starters(called, twins, goalies(), L)
    assert row(ambiguous, "MTL")["goalie_id"] == confirmed.UNKNOWN_GOALIE
