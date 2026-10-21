"""Policy v2's starters read only what was known before the decision (hard rule 1, ADR 0037): a
poll observed at or after the moment is never read, a new goalie's rating comes from rows known
before the cutoff, and a called team's goalie rows are known no earlier than the call."""

from datetime import UTC, date, datetime, timedelta

import polars as pl
from confirmed_fixtures import (
    BOBROVSKY_EFFECT,
    CUTOFF,
    GAME,
    TEAM_GAMES,
    at,
    candidates,
    dailyfaceoff,
    goal_multipliers,
    goalie_effects,
    goalie_starts,
    goalies,
    nhl,
)

from nhl_edge.audit import goalies as goalie_audit
from nhl_edge.lineup import confirmed

L = 0.85


def called(nhl_rows: list, dfo_rows: list) -> pl.DataFrame:
    return confirmed.calls(TEAM_GAMES, nhl(nhl_rows), dailyfaceoff(dfo_rows))


def test_a_poll_observed_at_the_moment_is_never_read() -> None:
    late = called(
        [("MTL", CUTOFF, 11)],
        [
            ("TOR", at(16, 0), "Joseph Woll", "Likely"),
            ("TOR", CUTOFF, "Anthony Stolarz", "Confirmed"),
        ],
    )
    mtl = late.filter(pl.col("team") == "MTL").row(0, named=True)
    tor = late.filter(pl.col("team") == "TOR").row(0, named=True)
    assert mtl["nhl_id"] is None and mtl["dfo_name"] is None
    assert (tor["dfo_name"], tor["dfo_status"]) == ("Joseph Woll", "Likely")
    named = confirmed.starters(late, candidates(), goalies(), L)
    assert named["team"].to_list() == ["TOR"] and named["source"].to_list() == [confirmed.LIKELY]


def test_a_new_goalies_rating_reads_only_rows_known_before_the_cutoff() -> None:
    named = confirmed.starters(
        called([], [("TOR", at(16, 30), "Sergei Bobrovsky", "Confirmed")]),
        candidates(),
        goalies(),
        L,
    )
    # A row of his known only at the cutoff moves nothing.
    _, effects, _ = confirmed.adjusted(
        goalie_starts(), goalie_effects(later=True), goal_multipliers(), named, CUTOFF
    )
    his = effects.filter(pl.col("game_id") == GAME, pl.col("goalie_id") == 30)
    assert his["effect"].to_list() == [BOBROVSKY_EFFECT]


def test_a_called_teams_goalie_rows_are_known_no_earlier_than_the_call() -> None:
    named = confirmed.starters(
        called([], [("MTL", at(16, 30), "Jakub Dobeš", "Likely")]), candidates(), goalies(), L
    )
    starts, _, _ = confirmed.adjusted(
        goalie_starts(), goalie_effects(), goal_multipliers(), named, CUTOFF
    )
    mtl = starts.filter(pl.col("team") == "MTL")
    assert mtl.height == 2 and (mtl["observed_utc"] >= at(16, 30)).all()
    assert (mtl["observed_utc"] < CUTOFF).all()
    # The other team's rows keep their own time.
    assert starts.filter(pl.col("team") == "TOR")["observed_utc"].equals(
        goalie_starts().filter(pl.col("team") == "TOR")["observed_utc"]
    )


def test_the_audits_decision_measure_reads_polls_before_1245_et_only() -> None:
    # 12:45 EDT on 2026-10-12 is 16:45 UTC: a Confirmed at 16:50 isn't the call at the decision.
    day = date(2026, 10, 12)
    start = datetime(2026, 10, 12, 23, 0, tzinfo=UTC)
    starters = pl.DataFrame(
        {
            "game_id": [GAME],
            "game_date": [day],
            "team": ["TOR"],
            "opponent": ["MTL"],
            "start_utc": [start],
            "starter_id": [20],
            "starter_name": ["Anthony Stolarz"],
            "surname_unique": [True],
        }
    )
    polls = dailyfaceoff(
        [
            ("TOR", at(16, 0), "Joseph Woll", "Likely"),
            ("TOR", at(16, 50), "Anthony Stolarz", "Confirmed"),
        ]
    )
    frame = goalie_audit.at_decision(starters, nhl([]), polls)
    row = frame.row(0, named=True)
    assert row["moment_utc"] == start - timedelta(hours=6, minutes=15)
    assert (row["dfo_name"], row["dfo_status"], row["dfo_right"]) == (
        "Joseph Woll",
        "Likely",
        False,
    )
