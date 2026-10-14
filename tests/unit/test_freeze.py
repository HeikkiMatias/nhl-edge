"""The frozen policy (#144, ADR 0030; v2 #222, ADR 0037). A failure here means a frozen number
changed: that is a new policy version, with its own freeze date and ADR, never an edit under the
old version."""

from dataclasses import fields
from datetime import date

from nhl_edge.backtest.market import Experiment
from nhl_edge.betting import guard, selection
from nhl_edge.betting.selection import POLICY, Policy
from nhl_edge.game import uncertainty


def test_the_frozen_policy_keeps_its_numbers() -> None:
    assert selection.POLICY_VERSION == "policy-20261014-e15db87"
    assert date(2026, 10, 14) == selection.FROZEN_ON
    assert selection.POLICY_V1 == "policy-20261005-8ec5cf3"
    assert (
        Policy(
            min_ev=0.025, ev_per_sd=0.01, kelly=0.25, max_bet=0.015, max_day=0.05, bankroll=100.0
        )
        == POLICY
    )
    assert guard.MOVE_THRESHOLD == 0.0535
    assert (guard.MORNING_SLOT, guard.DECISION_SLOT, guard.BOOK) == (
        "morning",
        "midday",
        "pinnacle",
    )
    assert uncertainty.ROOKIE_GAMES == 82
    # v2's L (ADR 0037): 45 of 45 Likely calls right at the decision, by the rule of succession.
    assert selection.LIKELY_STARTS == 46 / 47


def test_the_version_names_its_freeze_date() -> None:
    assert selection.POLICY_VERSION.split("-")[1] == selection.FROZEN_ON.strftime("%Y%m%d")
    assert date(2026, 10, 5) < selection.FROZEN_ON


def test_live_bets_use_the_blend_fitted_on_the_close_under_v1() -> None:
    # v2's B3 on history is v1's, so v2 reuses v1's live blend and attribution levels (ADR 0037).
    assert Experiment.E1.value == selection.LIVE_BLEND_EXPERIMENT
    assert selection.FIT_POLICY == selection.POLICY_V1


def test_u_reads_its_goalie_doubt_from_goalie_starts() -> None:
    # u's tables are v1's (ADR 0026): under v2 the day's starters reach u's goalie doubt only as
    # the slate games' goalie_starts rows (ADR 0037).
    assert [f.name for f in fields(uncertainty.Tables)] == [
        "games",
        "goalie_starts",
        "lineups",
        "lineup_replacements",
        "actual_lineups",
        "player_league_seasons",
    ]


def test_live_b3_and_u_read_the_days_starters_only_through_their_goalie_rows() -> None:
    # ADR 0037: a v2 decision reads the day's raw polls (live/goalies.py) and swaps each starter
    # into the slate games' goalie rows. No model table holds a poll, the run pulls none from the
    # lake, and the models' entry point is v1's.
    import inspect

    from nhl_edge.game import b2, b3
    from nhl_edge.live import goalies, predict

    confirmations = {"pregame_goalies", "dailyfaceoff_goalies"}
    for tables in (b2.Tables, b3.Tables, uncertainty.Tables):
        assert not confirmations & {f.name for f in fields(tables)}
    assert not confirmations & (set(predict.LAKE_TABLES) | set(goalies.TABLES))
    assert list(inspect.signature(predict.models).parameters) == [
        "tables",
        "b3_tables",
        "u_tables",
        "slate",
        "moments",
        "start",
        "season",
    ]
    assert selection.LIKELY_STARTS is not None
