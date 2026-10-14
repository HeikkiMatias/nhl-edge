"""Policy v2's starters in the day's decision (ADR 0037, #222): the day's polls replace the slate
games' goalie rows in the tables B2, B3 and u read, all three alike."""

import polars as pl
from confirmed_fixtures import (
    CUTOFF,
    DAY,
    GAME,
    SEASON,
    START,
    at,
    dailyfaceoff,
    goal_multipliers,
    goalie_effects,
    goalie_starts,
    goalies,
    nhl,
)

from nhl_edge.game import b2, b3, uncertainty
from nhl_edge.live import goalies as live_goalies

EMPTY = pl.DataFrame()
SLATE = pl.DataFrame(
    {
        "game_id": [GAME],
        "season": [SEASON],
        "game_date": [DAY],
        "start_utc": [START],
        "home": ["MTL"],
        "away": ["TOR"],
    }
)


def tables() -> tuple[b2.Tables, b3.Tables, uncertainty.Tables]:
    starts = goalie_starts()
    return (
        b2.Tables(EMPTY, EMPTY, EMPTY, starts, goalie_effects(), EMPTY),
        b3.Tables(
            EMPTY, EMPTY, starts, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, goal_multipliers()
        ),
        uncertainty.Tables(EMPTY, starts, EMPTY, EMPTY, EMPTY, EMPTY),
    )


def players() -> pl.DataFrame:
    return goalies().select(player_id="goalie_id", name="name")


def test_the_days_starters_replace_the_goalie_rows_all_three_models_read() -> None:
    polls = (
        nhl([]),
        dailyfaceoff(
            [
                ("MTL", at(16, 30), "Jakub Dobeš", "Confirmed"),
                ("TOR", at(16, 30), "Sergei Bobrovsky", "Likely"),
            ]
        ),
    )
    b2_t, b3_t, u_t, starters = live_goalies.with_starters(
        *tables(), SLATE, polls, players(), CUTOFF, 0.9
    )
    assert starters.select("team", "goalie_id", "p").rows() == [("MTL", 11, 1.0), ("TOR", 30, 0.9)]
    for starts in (b2_t.goalie_starts, b3_t.goalie_starts, u_t.goalie_starts):
        mtl = starts.filter(pl.col("team") == "MTL")
        assert mtl.select("goalie_id", "p_start").rows() == [(11, 1.0)]
    # Bobrovsky, outside TOR's candidates, is rated for B2 and faced by MTL's attack in B3.
    assert b2_t.goalie_effects.filter(pl.col("goalie_id") == 30, pl.col("game_id") == GAME).height
    assert b3_t.goal_multipliers.filter(pl.col("goalie_id") == 30, pl.col("team") == "MTL").height


def test_without_a_call_the_tables_are_unchanged() -> None:
    polls = (nhl([]), dailyfaceoff([("MTL", at(16, 30), "Jakub Dobeš", None)]))
    b2_t, b3_t, _, starters = live_goalies.with_starters(
        *tables(), SLATE, polls, players(), CUTOFF, 0.9
    )
    assert starters.is_empty()
    assert b2_t.goalie_starts.equals(goalie_starts())
    assert b3_t.goal_multipliers.equals(goal_multipliers())


def test_the_days_raw_polls_are_read_from_its_prefixes() -> None:
    assert live_goalies.raw_prefixes(DAY) == [
        "nhl/pregame-boxscore/2026-10-12/",
        "dailyfaceoff/starting-goalies/2026-10-12/",
    ]
