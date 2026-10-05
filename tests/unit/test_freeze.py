"""The frozen policy (#144, ADR 0030). A failure here means a frozen number changed: that is a new
policy version, with its own freeze date and ADR, never an edit under the old version."""

from dataclasses import fields
from datetime import date

from nhl_edge.backtest.market import Experiment
from nhl_edge.betting import guard, selection
from nhl_edge.betting.selection import POLICY, Policy
from nhl_edge.game import uncertainty


def test_the_frozen_policy_keeps_its_numbers() -> None:
    assert selection.POLICY_VERSION == "policy-20261005-8ec5cf3"
    assert date(2026, 10, 5) == selection.FROZEN_ON
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


def test_the_version_names_its_freeze_date() -> None:
    assert selection.POLICY_VERSION.split("-")[1] == selection.FROZEN_ON.strftime("%Y%m%d")


def test_live_bets_use_the_blend_fitted_on_the_close() -> None:
    assert Experiment.E1.value == selection.LIVE_BLEND_EXPERIMENT


def test_u_reads_no_starter_confirmations() -> None:
    # ADR 0030: live goalie doubt is the goalie-start model's, as on history.
    assert [f.name for f in fields(uncertainty.Tables)] == [
        "games",
        "goalie_starts",
        "lineups",
        "lineup_replacements",
        "actual_lineups",
        "player_league_seasons",
    ]
