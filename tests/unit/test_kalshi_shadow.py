"""Shadow kalshi_prices (#320, ADR 0046) on a SYNTHETIC day: the opportunities and both arms' fair
values, the taker part and the maker bid, the fill model, the re-valuation, the budget, the
measures, what is kept and the command."""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest
import shadow_fixtures as sf
from fakes import ConditionalBucket
from typer.testing import CliRunner

from nhl_edge.betting.selection import POLICY
from nhl_edge.cli import app
from nhl_edge.live import bundle as lb
from nhl_edge.live import exchange as ex
from nhl_edge.live import fills as lf
from nhl_edge.live import kalshi_shadow as ks
from nhl_edge.live import revalue as rv
from nhl_edge.market.recalibration import logit, sigmoid

WEIGHTS = (0.02, 1.05, 0.3)
BOOK_AT = sf.PUBLISHED - timedelta(minutes=1)
PRE7_BOOK = sf.PRE7 - timedelta(minutes=2)
FAIR = (1 / 1.80) / (1 / 1.80 + 1 / 2.10)  # WSH's fair probability at midday
MIDDAY_ODDS = [("midday", sf.MIDDAY, 1.80, 2.10)]


def run(
    books: list[Any],
    pairs: list[Any] | None = None,
    found: list[Any] = (),  # type: ignore[assignment]
    asks: list[Any] = (),  # type: ignore[assignment]
    polls: list[Any] = (),  # type: ignore[assignment]
    weights: Any = WEIGHTS,
    **ledger: Any,
) -> ks.DayResult:
    return ks.run_day(
        sf.DAY,
        sf.ledger(**ledger),
        sf.odds(pairs if pairs is not None else MIDDAY_ODDS),
        sf.depth(books),
        sf.trades(found),
        sf.tops(asks),
        sf.dailyfaceoff(polls),
        weights,
        sf.RULES,
    )


def of(result: ks.DayResult, arm: str = ks.MARKET, kind: str | None = None) -> pl.DataFrame:
    fills = result.fills.filter(pl.col("arm") == arm)
    return fills if kind is None else fills.filter(pl.col("kind") == kind)


def price_of(fills: pl.DataFrame) -> Decimal:
    """A maker fill's bid price, from its cost at the maker fee."""
    row = fills.row(0, named=True)
    contracts = Decimal(str(row["contracts"]))
    for cents in range(99, 0, -1):
        price = Decimal(cents) / 100
        if float(ex.cost(contracts, price, sf.RULES, maker=True)) == pytest.approx(row["cost_usd"]):
            return price
    raise AssertionError("no cent gives the fill's cost")


# The opportunities


def test_pinnacles_pair_is_de_vigged_and_a_pair_de_vigging_refuses_is_none() -> None:
    quoted = ks.quotes(sf.odds([*MIDDAY_ODDS, ("pre7", sf.PRE7, 2.10, 2.10)]), sf.DAY)
    assert quoted["slot"].to_list() == ["midday"]
    assert quoted["p_home"][0] == pytest.approx(FAIR)


def test_a_quote_older_than_five_minutes_is_no_opportunity() -> None:
    stale = sf.odds(MIDDAY_ODDS, age=timedelta(minutes=6))
    assert ks.opportunities(sf.ledger(), stale, sf.dailyfaceoff(), WEIGHTS, sf.DAY).is_empty()


def test_the_midday_slot_acts_once_the_decision_is_published() -> None:
    rows = ks.opportunities(sf.ledger(), sf.odds(MIDDAY_ODDS), sf.dailyfaceoff(), WEIGHTS, sf.DAY)
    assert set(rows["at_utc"].to_list()) == {sf.PUBLISHED}
    assert set(rows["arm"].to_list()) == {ks.MARKET, ks.HOCKEY}
    assert rows.height == 4  # two arms, two teams


def test_a_game_after_its_start_has_no_opportunity() -> None:
    late = [("pre7", sf.START + timedelta(minutes=1), 1.80, 2.10)]
    assert ks.opportunities(sf.ledger(), sf.odds(late), sf.dailyfaceoff(), None, sf.DAY).is_empty()


# The taker part


def test_the_taker_part_buys_the_asks_below_fair_value_within_the_stake() -> None:
    result = run([(BOOK_AT, sf.HOME, [(0.50, 10.0), (0.53, 20.0)], [(0.48, 5.0)])])
    taken = of(result, kind=ks.TAKER)
    assert taken.height == 1 and taken["fill_utc"][0] == sf.PUBLISHED
    contracts, cost = Decimal(str(taken["contracts"][0])), Decimal(str(taken["cost_usd"][0]))
    effective = float(contracts / cost)
    assert FAIR * effective - 1 >= POLICY.min_ev  # hard rule 4
    stake = float(cost) / sf.RULES.usd_per_eur
    assert stake <= ks.kelly(FAIR, 0.0)(effective) + ex.HALF_CENT + 1e-9
    assert stake <= POLICY.max_bet * ks.BANKROLL
    # only the cheap level: 0.53 doesn't clear the hurdle at the taker fee
    assert float(cost) == pytest.approx(float(ex.cost(contracts, Decimal("0.50"), sf.RULES)))


def test_the_taker_walks_the_levels_while_they_clear() -> None:
    allowed = ks.kelly(0.60, 0.0)
    contracts, cost, levels = ks.take(
        [(Decimal("0.50"), Decimal("0.50")), (Decimal("0.52"), Decimal("10"))],
        0.60,
        0.025,
        allowed,
        sf.RULES,
    )
    assert [price for price, _, _ in levels] == [Decimal("0.50"), Decimal("0.52")]
    assert contracts == sum(q for _, q, _ in levels) and cost == sum(k for _, _, k in levels)


def test_a_larger_taker_part_the_rounded_fee_lets_clear_is_kept() -> None:
    # Codex's case on #334: at 0.50 the fee's rounding up fails 1.47 contracts on the hurdle and
    # passes 1.48, which are within the stake, so the part is 1.48, not none.
    p, hurdle, price = 0.53044, 0.025, Decimal("0.50")
    allowed = ks.kelly(p, 0.0)
    contracts, cost, _ = ks.take([(price, Decimal("10"))], p, hurdle, allowed, sf.RULES)
    assert contracts == Decimal("1.48")
    effective = float(contracts / cost)
    assert p * effective - 1 >= hurdle
    assert float(cost) / sf.RULES.usd_per_eur + ex.HALF_CENT <= allowed(effective) + 1e-12
    smaller = Decimal("1.47")
    assert p * float(smaller / ex.cost(smaller, price, sf.RULES)) - 1 < hurdle


# The maker bid and its fills


def maker_day(found: list[Any] = (), asks: list[Any] = (), **kwargs: Any) -> ks.DayResult:  # type: ignore[assignment]
    # The ask at 0.52 is above fair value at the taker fee: no taker part; the bid rests a cent
    # below it, behind 5 contracts already bid there.
    return run(
        [(BOOK_AT, sf.HOME, [(0.52, 10.0)], [(0.51, 5.0), (0.49, 9.0)])],
        found=found,
        asks=asks,
        **kwargs,
    )


def test_an_ask_above_fair_value_leaves_a_maker_bid_a_cent_below_it() -> None:
    result = maker_day(found=[(sf.PUBLISHED + timedelta(hours=1), sf.HOME, 0.50, 100.0)])
    assert of(result, kind=ks.TAKER).is_empty()
    made = of(result, kind=ks.MAKER)
    price = price_of(made)
    assert price == Decimal("0.51")  # its own limit would cross the ask at 0.52
    contracts = Decimal(str(made["contracts"].sum()))
    cost = ex.cost(contracts, price, sf.RULES, maker=True)
    assert FAIR * float(contracts / cost) - 1 >= POLICY.min_ev


def test_a_maker_bid_fills_only_after_the_contracts_ahead_of_it_trade() -> None:
    first, second = sf.PUBLISHED + timedelta(hours=1), sf.PUBLISHED + timedelta(hours=2)
    result = maker_day(found=[(first, sf.HOME, 0.51, 3.0), (second, sf.HOME, 0.51, 3.0)])
    made = of(result, kind=ks.MAKER)
    assert made["fill_utc"].to_list() == [second]
    assert made["contracts"][0] == pytest.approx(1.0)  # 6 traded, 5 were ahead: a partial fill


def test_an_ask_reaching_the_bid_fills_what_is_left() -> None:
    crossed = sf.PUBLISHED + timedelta(hours=3)
    result = maker_day(asks=[(crossed, sf.HOME, 0.51)])
    made = of(result, kind=ks.MAKER)
    assert made["fill_utc"].to_list() == [crossed]
    assert result.counts[ks.MARKET]["bids"]["filled"] == 1
    assert made["contracts"][0] > 0


def test_a_bid_expires_five_minutes_before_the_start() -> None:
    result = maker_day(found=[(sf.START - timedelta(minutes=2), sf.HOME, 0.40, 100.0)])
    assert of(result, kind=ks.MAKER).is_empty()
    assert result.counts[ks.MARKET]["bids"]["expired"] == 1


# The re-valuation


def test_a_resting_bid_is_lowered_when_pinnacle_moves_against_it() -> None:
    later = sf.PRE7 + timedelta(minutes=5)
    result = maker_day(
        pairs=[*MIDDAY_ODDS, ("pre7", sf.PRE7, 2.00, 1.90)],
        found=[(later, sf.HOME, 0.51, 50.0), (later + timedelta(minutes=1), sf.HOME, 0.40, 50.0)],
    )
    assert result.counts[ks.MARKET]["bids"]["lowered"] == 1
    made = of(result, kind=ks.MAKER)
    assert made["fill_utc"].to_list() == [later + timedelta(minutes=1)]  # not at its old price
    assert price_of(made) < Decimal("0.51")


def test_a_resting_bid_is_never_raised() -> None:
    later = sf.PRE7 + timedelta(minutes=5)
    result = maker_day(
        pairs=[*MIDDAY_ODDS, ("pre7", sf.PRE7, 1.60, 2.55)],
        found=[(later, sf.HOME, 0.51, 50.0)],
    )
    assert "lowered" not in result.counts[ks.MARKET]["bids"]
    assert price_of(of(result, kind=ks.MAKER)) == Decimal("0.51")


def test_a_bid_no_cent_clears_for_is_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rv, "lowered", lambda *args: None)
    later = sf.PRE7 + timedelta(minutes=5)
    result = maker_day(
        pairs=[*MIDDAY_ODDS, ("pre7", sf.PRE7, 2.00, 1.90)], found=[(later, sf.HOME, 0.30, 50.0)]
    )
    assert result.counts[ks.MARKET]["bids"]["cancelled"] == 1
    assert of(result, kind=ks.MAKER).is_empty()


# The budget


def test_one_team_per_game_the_first_to_clear() -> None:
    result = run(
        [
            (BOOK_AT, sf.HOME, [(0.45, 10.0)], []),
            (BOOK_AT, sf.AWAY, [(0.40, 10.0)], []),
        ]
    )
    market = result.opportunities.filter(pl.col("arm") == ks.MARKET)
    assert sorted(market["action"].to_list()) == sorted([ks.TOOK, ks.OTHER_TEAM])
    assert set(of(result)["team"].to_list()) == {sf.HOME}


def test_every_hook_on_a_game_stays_within_max_bet() -> None:
    result = run(
        [
            (BOOK_AT, sf.HOME, [(0.40, 100.0)], []),
            (PRE7_BOOK, sf.HOME, [(0.35, 100.0)], []),
        ],
        pairs=[*MIDDAY_ODDS, ("pre7", sf.PRE7, 1.80, 2.10)],
    )
    spent = float(of(result)["cost_usd"].sum()) / sf.RULES.usd_per_eur
    assert spent <= POLICY.max_bet * ks.BANKROLL + ex.HALF_CENT


def test_the_day_caps_every_game_together() -> None:
    state = ks.ArmDay(ks.MARKET, sf.RULES)
    state.spend(1, Decimal(str(4.9 * sf.RULES.usd_per_eur)))
    assert state.room(2) == pytest.approx(POLICY.max_day * ks.BANKROLL - 4.9)
    assert state.room(1) == 0.0  # its own game is past max_bet


def test_a_bid_cancelled_or_expired_frees_what_it_left_unfilled() -> None:
    state = ks.ArmDay(ks.MARKET, sf.RULES)
    bid = ks.Bid(
        game_id=sf.GAME,
        team=sf.HOME,
        p_side=0.55,
        hurdle=0.025,
        price=Decimal("0.50"),
        left=Decimal("2.00"),
        posted_utc=sf.PUBLISHED,
        queue=Decimal(0),
        expires_utc=sf.START - sf.RULES.expire_before,
        placed_utc=sf.PUBLISHED,
    )
    state.bids[(sf.GAME, sf.HOME)] = bid
    state.spend(sf.GAME, bid.planned(sf.RULES))
    filled = ks.advance(
        bid,
        bid.expires_utc,
        ks.Market((), ((sf.PUBLISHED + timedelta(hours=1), Decimal("0.50"), Decimal("0.5")),), ()),
        sf.RULES,
    )
    assert [q for _, q, _ in filled] == [Decimal("0.5")]
    ks._release(state, (sf.GAME, sf.HOME), bid, "expired")
    kept = float(bid.filled_cost) / sf.RULES.usd_per_eur
    assert state.day_exposure == pytest.approx(kept) and state.games[sf.GAME] == pytest.approx(kept)
    assert state.counts == {"expired": 1} and not state.bids


def test_no_fresh_book_no_hook() -> None:
    result = run(
        [(sf.PUBLISHED - ks.MAX_BOOK_AGE - timedelta(seconds=1), sf.HOME, [(0.40, 10.0)], [])]
    )
    assert result.fills.is_empty()
    assert set(result.opportunities["action"].to_list()) == {ks.NO_BOOK}


# The hockey arm


def test_the_hockey_arm_is_conservative_blend_at_the_slots_market_price() -> None:
    rows = ks.opportunities(sf.ledger(), sf.odds(MIDDAY_ODDS), sf.dailyfaceoff(), WEIGHTS, sf.DAY)
    home = rows.filter(pl.col("arm") == ks.HOCKEY, pl.col("team") == sf.HOME).row(0, named=True)
    a, b_m, b_d = WEIGHTS
    market = logit(np.array([FAIR]))
    expected = sigmoid(a + b_m * market + b_d * (logit(np.array([0.58])) - market))[0]
    assert home["p_side"] == pytest.approx(expected)
    assert home["hurdle"] == pytest.approx(POLICY.min_ev + POLICY.ev_per_sd * 0.5)


def test_an_unfitted_week_has_no_hockey_opportunities() -> None:
    result = run([(BOOK_AT, sf.HOME, [(0.45, 10.0)], [])], weights=None)
    assert set(result.opportunities["arm"].to_list()) == {ks.MARKET}
    assert result.counts["fitted"] is False


def test_a_starter_change_takes_the_game_out_of_the_hockey_arm_and_cancels_its_bid() -> None:
    polls = [
        (sf.MIDDAY - timedelta(minutes=10), sf.HOME, "Goalie One"),
        (sf.MIDDAY - timedelta(minutes=10), sf.AWAY, "Goalie Two"),
        (sf.PRE7 - timedelta(minutes=10), sf.HOME, "Goalie Three"),
        (sf.PRE7 - timedelta(minutes=10), sf.AWAY, "Goalie Two"),
    ]
    result = maker_day(pairs=[*MIDDAY_ODDS, ("pre7", sf.PRE7, 1.80, 2.10)], polls=polls)
    hockey = result.opportunities.filter(pl.col("arm") == ks.HOCKEY)
    assert set(hockey["slot"].to_list()) == {"midday"}
    assert result.counts[ks.HOCKEY]["bids"]["cancelled"] == 1
    market = result.opportunities.filter(pl.col("arm") == ks.MARKET)
    assert "pre7" in market["slot"].to_list()


def test_a_starter_change_cancels_a_resting_hockey_bid_without_a_fresh_pair() -> None:
    # Codex's case on #334: pre7's pair is stale, so pre7 has no opportunity, yet the starter
    # named by then cancels the hockey bid, as a real one's re-valuation checks the starters
    # before any quote; a later trade at its price fills only the market arm's.
    polls = [
        (sf.MIDDAY - timedelta(minutes=10), sf.HOME, "Goalie One"),
        (sf.MIDDAY - timedelta(minutes=10), sf.AWAY, "Goalie Two"),
        (sf.PRE7 - timedelta(minutes=10), sf.HOME, "Goalie Three"),
    ]
    odds = pl.concat(
        [
            sf.odds(MIDDAY_ODDS),
            sf.odds([("pre7", sf.PRE7, 1.80, 2.10)], age=timedelta(minutes=10)),
        ]
    )
    result = ks.run_day(
        sf.DAY,
        sf.ledger(),
        odds,
        sf.depth([(BOOK_AT, sf.HOME, [(0.52, 10.0)], [(0.51, 5.0), (0.49, 9.0)])]),
        sf.trades([(sf.PRE7 + timedelta(minutes=5), sf.HOME, 0.40, 50.0)]),
        sf.tops(),
        sf.dailyfaceoff(polls),
        WEIGHTS,
        sf.RULES,
    )
    assert "pre7" not in result.opportunities["slot"].to_list()
    assert result.counts[ks.HOCKEY]["bids"]["cancelled"] == 1
    assert of(result, ks.HOCKEY, ks.MAKER).is_empty()
    assert of(result, ks.MARKET, ks.MAKER).height == 1


# The measures


def referenced(rows: list[dict[str, Any]]) -> pl.DataFrame:
    base = {
        "game_date": sf.DAY,
        "season": sf.SEASON,
        "game_id": sf.GAME,
        "team": sf.HOME,
        "start_utc": sf.START,
        "placed_utc": sf.PUBLISHED,
        "fill_utc": sf.PUBLISHED,
        "kind": ks.TAKER,
        "contracts": 2.0,
        "cost_usd": 1.0,
        "arm": ks.MARKET,
        "starter_changed": None,
        "moved_against": None,
        "reference": lf.VALUED,
        "p_close": 0.55,
    }
    return pl.DataFrame(
        [base | r for r in rows],
        schema=ks.FILL_SCHEMA | {"reference": pl.String, "p_close": pl.Float64},
    )


def opportunities(arms: tuple[str, ...]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "arm": arm,
                "slot": "midday",
                "at_utc": sf.PUBLISHED,
                "game_date": sf.DAY,
                "season": sf.SEASON,
                "game_id": sf.GAME,
                "team": sf.HOME,
                "start_utc": sf.START,
                "p_side": 0.55,
                "hurdle": 0.025,
                "action": ks.TOOK,
            }
            for arm in arms
        ],
        schema=ks.OPPORTUNITY_SCHEMA,
    )


def test_a_bid_is_valued_against_the_proxy_after_it_and_a_later_fill_counted_apart() -> None:
    rows = referenced([{}, {"reference": lf.AFTER_REFERENCE, "kind": ks.MAKER}])
    meta = [{"day": sf.DAY.isoformat(), "counts": {"fitted": False, "watch_logs": True}}]
    figures = ks.summary(rows, opportunities((ks.MARKET,)), meta, sf.RULES.usd_per_eur)
    taker = figures[ks.MARKET][ks.TAKER]
    assert taker["bids"] == 1 and taker["fills"][lf.VALUED] == 1
    assert taker["profit_eur"] == pytest.approx((0.55 * 2.0 - 1.0) / sf.RULES.usd_per_eur)
    assert taker["clv_per_bid"] == {"games": 1, "weeks": 1}  # counts only under 4 weeks
    maker = figures[ks.MARKET][ks.MAKER]
    assert maker["bids"] == 0 and maker["fills"][lf.AFTER_REFERENCE] == 1


def test_the_paired_difference_counts_only_days_both_arms_ran() -> None:
    bids = ks.valued(referenced([{"arm": ks.HOCKEY, "p_close": 0.60}, {}]), sf.RULES.usd_per_eur)
    both = opportunities((ks.MARKET, ks.HOCKEY))
    paired = ks.paired(bids, both, [sf.DAY])
    expected = ((0.60 * 2 - 1) - (0.55 * 2 - 1)) / sf.RULES.usd_per_eur
    assert paired["difference"].to_list() == [pytest.approx(expected)]
    assert ks.paired(bids, both, []).is_empty()


def test_the_maker_fills_adverse_shares() -> None:
    rows = referenced(
        [
            {"kind": ks.MAKER, "starter_changed": True, "moved_against": False},
            {"kind": ks.MAKER, "starter_changed": False, "moved_against": True},
            {"kind": ks.MAKER, "starter_changed": None, "moved_against": True},
        ]
    )
    meta = [{"day": sf.DAY.isoformat(), "counts": {}}]
    adverse = ks.summary(rows, opportunities((ks.MARKET,)), meta, 1.16)[ks.MARKET]["adverse"]
    assert adverse["starter_changed"] == {"fills": 2, "of_them": 1}
    assert adverse["moved_against"] == {"fills": 3, "of_them": 2}


def test_a_fill_after_pinnacle_moved_against_its_side_is_marked() -> None:
    later = sf.PRE7 + timedelta(minutes=5)
    result = maker_day(
        pairs=[*MIDDAY_ODDS, ("pre7", sf.PRE7, 1.95, 1.95)], found=[(later, sf.HOME, 0.40, 50.0)]
    )
    made = of(result, kind=ks.MAKER)
    assert made["moved_against"].to_list() == [True]


def test_a_fill_whose_starters_were_never_named_is_unknown_not_unchanged() -> None:
    # Codex's case on #334: with no Daily Faceoff poll the starter share must not count the fill
    # as one without a change; with both goalies named and the same, it does.
    found = [(sf.PUBLISHED + timedelta(hours=1), sf.HOME, 0.40, 50.0)]
    unnamed = of(maker_day(found=found), kind=ks.MAKER)
    assert unnamed.height == 1 and unnamed["starter_changed"].to_list() == [None]
    polls = [
        (sf.MIDDAY - timedelta(minutes=10), sf.HOME, "Goalie One"),
        (sf.MIDDAY - timedelta(minutes=10), sf.AWAY, "Goalie Two"),
    ]
    named = of(maker_day(found=found, polls=polls), kind=ks.MAKER)
    assert named["starter_changed"].to_list() == [False]


def test_a_fill_of_a_game_without_a_decision_row_reads_its_teams_from_the_odds() -> None:
    # Codex's case on #334: its side and reference need the game's teams, from the odds' listing.
    assert ks.teams(sf.ledger().clear(), sf.odds(MIDDAY_ODDS)).rows() == [
        (sf.GAME, sf.HOME, sf.AWAY)
    ]
    later = sf.PRE7 + timedelta(minutes=5)
    result = ks.run_day(
        sf.DAY,
        sf.ledger().clear(),
        sf.odds([*MIDDAY_ODDS, ("pre7", sf.PRE7, 1.95, 1.95)]),
        sf.depth([(sf.MIDDAY, sf.HOME, [(0.52, 10.0)], [(0.51, 5.0), (0.49, 9.0)])]),
        sf.trades([(later, sf.HOME, 0.40, 50.0)]),
        sf.tops(),
        sf.dailyfaceoff(),
        WEIGHTS,
        sf.RULES,
    )
    # WSH's fair price fell from midday to pre7: against the home side it was bid on
    assert of(result, kind=ks.MAKER)["moved_against"].to_list() == [True]


# What is kept


def test_a_day_is_kept_once_and_read_back() -> None:
    store = lb.R2Store(ConditionalBucket(), "bucket")
    result = maker_day(found=[(sf.PUBLISHED + timedelta(hours=1), sf.HOME, 0.50, 100.0)])
    now = sf.START + timedelta(hours=12)
    ks.keep(store, sf.DAY, result, "kalshi-shadow-20261028-abc1234", now)
    assert ks.stored_days(store) == [sf.DAY]
    found, opportunities_kept, metas = ks.kept(store)
    assert found.height == result.fills.height and opportunities_kept.height
    assert metas[0]["counts"] == json.loads(json.dumps(result.counts))
    with pytest.raises(lb.Conflict):
        ks.keep(store, sf.DAY, result, "kalshi-shadow-20261028-def5678", now)
    assert all(key.startswith(ks.PREFIX) for key in store.keys("real/shadow"))


def test_the_days_due_are_from_the_first_day_before_today_not_kept() -> None:
    today = ks.FIRST_DAY + timedelta(days=3)
    assert ks.due(today, [ks.FIRST_DAY + timedelta(days=1)]) == [
        ks.FIRST_DAY,
        ks.FIRST_DAY + timedelta(days=2),
    ]
    assert ks.due(ks.FIRST_DAY, []) == []


def test_the_weeks_fit_is_conservative_blends_or_none_under_its_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC

    from nhl_edge.live import challengers

    rng = np.random.default_rng(7)

    def training(n: int) -> pl.DataFrame:
        p_b0 = rng.uniform(0.35, 0.65, n)
        return pl.DataFrame(
            {
                "p_b0": p_b0,
                "p_b3": np.clip(p_b0 + rng.normal(0, 0.03, n), 0.05, 0.95),
                "home_win": (rng.uniform(0, 1, n) < p_b0).astype(int),
                "result_utc": [datetime(2026, 10, 20, 10, tzinfo=UTC)] * n,
            }
        )

    few = training(challengers.MINIMUM_GAMES - 1)
    monkeypatch.setattr(challengers, "training_rows", lambda ledger, games, day: few)
    fit = ks.week_fit(sf.ledger(), pl.DataFrame(), sf.DAY)
    assert ks.weights_of(fit) is None and fit["games"] == challengers.MINIMUM_GAMES - 1
    assert "train_cutoff" not in fit
    enough = training(challengers.MINIMUM_GAMES)
    monkeypatch.setattr(challengers, "training_rows", lambda ledger, games, day: enough)
    fit = ks.week_fit(sf.ledger(), pl.DataFrame(), sf.DAY)
    expected, _ = challengers.conservative_fit(enough["home_win"], enough["p_b0"], enough["p_b3"])
    assert ks.weights_of(fit) == pytest.approx(tuple(expected))
    assert fit["week"] == "2026-10-26" and fit["train_cutoff"] < fit["fold_start_utc"]


def test_a_game_without_a_decision_row_is_market_only() -> None:
    rows = ks.opportunities(
        sf.ledger().clear(), sf.odds(MIDDAY_ODDS), sf.dailyfaceoff(), WEIGHTS, sf.DAY
    )
    assert set(rows["arm"].to_list()) == {ks.MARKET} and rows.height == 2
    first = rows.row(0, named=True)
    assert first["start_utc"] == sf.START and first["season"] == sf.SEASON
    assert first["at_utc"] == sf.MIDDAY  # no publication to wait for


def test_a_later_days_game_in_the_snapshot_is_no_opportunity() -> None:
    # Codex's case on #334: a slot's snapshot lists the next days' games too; without a decision
    # row of the day, only a game the listing starts on the day is one.
    tomorrow = sf.odds(MIDDAY_ODDS).with_columns(
        game_id=pl.lit(sf.GAME + 1, pl.Int64),
        commence_time_utc=pl.lit(sf.START + timedelta(days=1)).cast(sf.UTC_US),
    )
    odds = pl.concat([sf.odds(MIDDAY_ODDS), tomorrow])
    rows = ks.opportunities(sf.ledger(), odds, sf.dailyfaceoff(), WEIGHTS, sf.DAY)
    assert set(rows["game_id"].to_list()) == {sf.GAME}


def test_another_policys_decision_has_no_hockey_arm() -> None:
    rows = ks.opportunities(
        sf.ledger(policy_version="policy-20261005-8ec5cf3"),
        sf.odds(MIDDAY_ODDS),
        sf.dailyfaceoff(),
        WEIGHTS,
        sf.DAY,
    )
    assert set(rows["arm"].to_list()) == {ks.MARKET}


# The command


class Lake:
    def __init__(self, tables: dict[str, pl.DataFrame]) -> None:
        self.tables = tables

    def pull(self, table: str) -> int:
        return 0

    def read(self, table: str, seasons: Any = None) -> pl.DataFrame:
        return self.tables[table]


def test_nhl_real_shadow_keeps_each_day_and_prints_counts_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nhl_edge import cli

    store = lb.R2Store(ConditionalBucket(), "bucket")
    tables = {
        "odds_snapshots": sf.odds(MIDDAY_ODDS),
        "paper_ledger": sf.ledger(),
        "games": pl.DataFrame(),
        "dailyfaceoff_goalies": sf.dailyfaceoff(),
        "kalshi_depth": sf.depth([(BOOK_AT, sf.HOME, [(0.50, 10.0)], [(0.48, 5.0)])]),
        "kalshi_trades": sf.trades(),
        "kalshi_tops": sf.tops(),
    }
    monkeypatch.setattr(cli, "_real_record", lambda r2: (Lake(tables), store))
    fit = {"week": "2026-10-26", "games": 250, "weights": list(WEIGHTS)}
    monkeypatch.setattr(ks, "week_fit", lambda ledger, games, day: fit)
    monkeypatch.setattr(ks, "FIRST_DAY", sf.DAY)

    clock = [sf.START + timedelta(days=1)]

    class Day(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return clock[0]

    monkeypatch.setattr(cli, "datetime", Day)
    result = CliRunner().invoke(app, ["real", "shadow"])
    assert result.exit_code == 0, result.output
    assert "kalshi shadow: 1 days computed now, 1 kept" in result.output
    for secret in ("0.5", "$", "€", sf.HOME, sf.TICKERS[sf.HOME]):
        assert secret not in result.output
    reports = store.keys(f"{ks.PREFIX}/reports")
    assert len(reports) == 1
    figures = json.loads(store.get(reports[0]))
    assert figures["days"] == 1 and figures[ks.MARKET][ks.TAKER]["fills"][lf.PENDING] == 0
    _, _, metas = ks.kept(store)
    assert metas[0]["fit"] == fit and metas[0]["counts"]["fitted"] is True
    # the next night computes nothing new
    clock[0] += timedelta(days=1)
    monkeypatch.setattr(ks, "due", lambda today, done: [d for d in [sf.DAY] if d not in done])
    again = CliRunner().invoke(app, ["real", "shadow"])
    assert "0 days computed now, 1 kept" in again.output


def test_the_first_day_is_the_adrs() -> None:
    assert date(2026, 10, 24) == ks.FIRST_DAY
