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


def test_live_b3_and_u_read_no_starter_confirmations() -> None:
    # ADR 0030, as amended on 2026-10-05: a starter confirmed before the decision moves neither
    # live p_b3 nor u. Live B2, B3 and u read the goalie-start model's likely starters only, so
    # no table they read, and no table a decision pulls, holds a confirmation.
    import inspect

    from nhl_edge.game import b2, b3
    from nhl_edge.live import predict

    confirmations = {"pregame_goalies", "dailyfaceoff_goalies"}
    for tables in (b2.Tables, b3.Tables, uncertainty.Tables):
        assert not confirmations & {f.name for f in fields(tables)}
    assert not confirmations & set(predict.LAKE_TABLES)
    assert list(inspect.signature(predict.models).parameters) == [
        "tables",
        "b3_tables",
        "u_tables",
        "slate",
        "moments",
        "start",
        "season",
    ]
