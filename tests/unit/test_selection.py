from datetime import date

import polars as pl
import pytest
from golden_games import CASES, parse_case

from nhl_edge.backtest.walk_forward import outcomes
from nhl_edge.betting import selection, staking
from nhl_edge.betting.selection import POLICY

DAY = date(2021, 11, 10)


def games(**columns: list[object]) -> pl.DataFrame:
    n = len(next(iter(columns.values())))
    base: dict[str, list[object]] = {
        "season": [20212022] * n,
        "game_id": list(range(1, n + 1)),
        "game_date": [DAY] * n,
        "u_sd": [0.0] * n,
    }
    return pl.DataFrame(base | columns)


def test_the_gap_is_never_read_only_the_expected_return() -> None:
    # Plan §5: a 52.5% blend against a 50% fair price at 1.90 is a 2.5-point gap, but its
    # expected return is -0.25%.
    picked = selection.select(games(p_home=[0.525], home_price=[1.90], away_price=[1.90]))
    row = picked.row(0, named=True)
    assert row["side"] == "home"
    assert row["ev"] == pytest.approx(-0.0025)
    assert not row["picked"]


def test_the_side_of_higher_expected_return_is_picked_above_the_hurdle() -> None:
    picked = selection.select(
        games(
            p_home=[0.40, 0.60, 0.60], home_price=[2.20, 1.80, 1.55], away_price=[1.70, 2.10, 2.40]
        )
    )
    assert picked["side"].to_list() == ["away", "home", "away"]
    # 0.60·1.80 = 1.08: an 8% return. 0.40·2.40 = 0.96: -4% beats 0.60·1.55 = 0.93, but no bet.
    assert picked["ev"].to_list() == pytest.approx([0.02, 0.08, -0.04])
    assert picked["picked"].to_list() == [False, True, False]


def test_doubt_raises_the_hurdle_one_point_per_standard_deviation() -> None:
    picked = selection.select(
        games(
            p_home=[0.55, 0.55, 0.55],
            home_price=[1.90, 1.90, 1.90],
            away_price=[2.00, 2.00, 2.00],
            u_sd=[-1.0, 1.0, 1.9],
        )
    )
    # EV 4.5%: hurdles 2.5% (below average doubt counts as average), 3.5% and 4.4%.
    assert picked["hurdle"].to_list() == pytest.approx([0.025, 0.035, 0.044])
    assert picked["picked"].to_list() == [True, True, True]
    assert not selection.select(
        games(p_home=[0.55], home_price=[1.90], away_price=[2.00], u_sd=[2.5])
    )["picked"][0]


def test_quarter_kelly_shrinks_with_doubt_and_is_capped_per_bet_and_per_day() -> None:
    bets = games(
        ev=[0.04, 0.04, 0.20],
        price=[2.0, 2.0, 2.0],
        u_sd=[0.0, 1.0, 0.0],
    )
    shares = staking.fractions(bets)["fraction"].to_list()
    # 0.25·0.04/1 = 1%, halved at one standard deviation of doubt; 5% is capped at 1.5%.
    assert shares == pytest.approx([0.01, 0.005, 0.015])
    many = games(ev=[0.10] * 6, price=[2.0] * 6)
    day = staking.fractions(many)["fraction"]
    assert day.sum() == pytest.approx(POLICY.max_day)
    assert day.to_list() == pytest.approx([POLICY.max_day / 6] * 6)


def test_a_days_stakes_read_the_bankroll_left_by_earlier_days() -> None:
    bets = pl.DataFrame(
        {
            "season": [20212022] * 3,
            "game_id": [1, 2, 3],
            "game_date": [DAY, DAY, date(2021, 11, 11)],
            "side": ["home", "away", "home"],
            "price": [2.0, 3.0, 2.0],
            "ev": [0.04, 0.04, 0.04],
            "u_sd": [0.0, 0.0, 0.0],
            "home_win": [1, 1, 0],
        }
    )
    ledger = staking.settle(bets)
    # Day one: 1% and 0.5% of 100; the home bet wins 1, the away bet loses 0.5.
    assert ledger["stake"].to_list()[:2] == pytest.approx([1.0, 0.5])
    assert ledger["profit"].to_list()[:2] == pytest.approx([1.0, -0.5])
    # Day two stakes 1% of 100.5 and loses it.
    assert ledger["bankroll_before"].to_list() == pytest.approx([100.0, 100.0, 100.5])
    assert ledger["profit"][2] == pytest.approx(-1.005)
    assert staking.drawdown(ledger) == pytest.approx(1.005 / 100.5)


@pytest.mark.parametrize(
    ("case", "home_bet_wins"),
    [("regulation", True), ("overtime", True), ("shootout", False), ("late_empty_net", True)],
)
def test_bets_settle_on_the_full_game(case: str, home_bet_wins: bool) -> None:
    # The golden games: VGK beat SEA in regulation, BUF beat TBL in overtime, CAR won the
    # shootout at LAK, and FLA's late empty-net goal sealed its win over TOR (hard rule 2).
    result = outcomes(parse_case(CASES[case])["games"])
    for side, wins in (("home", home_bet_wins), ("away", not home_bet_wins)):
        bet = result.select(
            "game_id",
            "home_win",
            season=pl.lit(20212022),
            game_date=pl.lit(DAY),
            side=pl.lit(side),
            price=pl.lit(2.0),
            ev=pl.lit(0.04),
            u_sd=pl.lit(0.0),
        )
        ledger = staking.settle(bet)
        assert ledger["win"][0] is wins
        assert ledger["profit"][0] == pytest.approx(1.0 if wins else -1.0)
