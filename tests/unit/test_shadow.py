"""Shadow evaluations on the live ledger (#252): the frozen rules on the market-only control, and
the policy's own bets at the best fresh price of the other books."""

from datetime import UTC, datetime, timedelta

import polars as pl
import predict_fixtures as pf
import pytest

from nhl_edge.live import movement, shadow
from nhl_edge.live import predict as lp
from nhl_edge.live import settle as ls

PRE7 = datetime(2026, 10, 7, 22, 45, 30, tzinfo=UTC)  # 18:45 ET, 45 minutes before 053 and 054
NOW = datetime(2026, 10, 8, 9, tzinfo=UTC)
WSH, WPG, ANA = 2026020053, 2026020054, 2026020055
KEYS = ["game_date", "game_id"]


def ledger() -> pl.DataFrame:
    """The fixture day's ledger: a home bet on each of its three games."""
    inputs = pf.day()
    return lp.ledger(lp.decide(inputs), inputs)


def odds(*extra: list[dict[str, object]]) -> pl.DataFrame:
    """The day's quotes (Pinnacle, and bet365 for 053 at 2.15) and a pre7 snapshot of 053 and
    054 at Pinnacle, as the lake keeps them."""
    rows = pl.concat(
        [
            pf.day_quotes(),
            pf.quotes(
                pf.quote(PRE7, WSH, "pinnacle", 2.0, 1.9, slot="pre7"),
                pf.quote(PRE7, WPG, "pinnacle", 1.8, 2.1, slot="pre7"),
                *extra,
            ),
        ]
    )
    return rows.with_columns(game_id=pl.col("event_id").str.strip_prefix("e").cast(pl.Int64))


def moves(decided: pl.DataFrame, quotes: pl.DataFrame) -> pl.DataFrame:
    return movement.table(decided, ls.closes(quotes, NOW))


def test_the_market_only_shadow_applies_the_frozen_rules_unchanged() -> None:
    # With the market-only control equal to the blend, the shadow makes the policy's own bets.
    decided = ledger()
    same = shadow.market_only(decided.with_columns(p_blend_market=pl.col("p_blend")))
    own = decided.filter(pl.col("bet"))
    columns = ["game_id", "side", "price", "ev", "fraction", "stake"]
    assert same.select(columns).sort("game_id").equals(own.select(columns).sort("game_id"))
    # The market-only control's own bets are chosen on its probability, at the same prices.
    market = shadow.market_only(decided)
    for row in market.iter_rows(named=True):
        p = row["p_side"]
        assert row["ev"] == pytest.approx(p * row["price"] - 1)
        assert row["ev"] >= row["hurdle"]


def test_the_frozen_rules_on_the_blend_itself_make_the_policys_own_bets() -> None:
    # shadow.selected, as a challenger's shadow bets use it (ADR 0038): on p_blend, the ledger's
    # own bets, and on the market-only control, #252's.
    decided = ledger()
    own = decided.filter(pl.col("bet"))
    columns = ["game_id", "side", "price", "ev", "hurdle", "fraction", "stake"]
    again = shadow.selected(decided, "p_blend")
    assert again.select(columns).sort("game_id").equals(own.select(columns).sort("game_id"))
    assert shadow.selected(decided, "p_blend_market").equals(shadow.market_only(decided))


def test_a_shadow_bet_is_valued_as_settlement_values_the_policys() -> None:
    decided = ledger()
    quotes = odds()
    own = shadow.valued(decided.filter(pl.col("bet")), moves(decided, quotes))
    settled = ls.settle(decided, settle_games(), quotes, NOW, {"code_version": "x"})
    both = own.join(settled.select(*KEYS, settled_clv="clv"), on=KEYS)
    # ANA has no close, so only WSH and WPG are valued, as settlement values them.
    assert sorted(both["game_id"].to_list()) == [WSH, WPG]
    assert both["clv"].to_list() == pytest.approx(both["settled_clv"].to_list())


def settle_games() -> pl.DataFrame:
    rows = []
    for game_id, (home, away) in {WSH: (3, 2), WPG: (1, 4), ANA: (4, 1)}.items():
        start = pf.GAMES[game_id][2]
        rows.append(
            {
                "game_id": game_id,
                "start_utc": start,
                "home_score": home,
                "away_score": away,
                "observed_utc": start + timedelta(hours=12),
            }
        )
    return pl.DataFrame(rows, schema_overrides={"home_score": pl.Int16, "away_score": pl.Int16})


def test_the_best_fresh_price_beats_pinnacles_and_exchanges_stay_apart() -> None:
    decided = ledger()
    stale = pf.MIDDAY - timedelta(minutes=10)
    quotes = odds(
        # A stale bookmaker quote above bet365's is no executable price.
        pf.quote(pf.MIDDAY, WSH, "williamhill", 2.30, 1.70, updated=stale),
        # An exchange beats Pinnacle's 1.90 on WPG's home side, before its commission.
        pf.quote(pf.MIDDAY, WPG, "matchbook", 2.00, 1.95),
    )
    priced = shadow.best_price(decided.filter(pl.col("bet")), quotes, moves(decided, quotes))
    rows = {row["game_id"]: row for row in priced.iter_rows(named=True)}
    wsh, wpg = rows[WSH], rows[WPG]
    assert (wsh["best_bookmaker"], wsh["best_bookmaker_book"]) == (2.15, "bet365")
    assert wsh["better_bookmaker"] and wsh["clv_bookmaker"] == pytest.approx(
        2.15 * wsh["p_close_side"] - 1
    )
    assert wsh["gain_bookmaker"] == pytest.approx((2.15 - 2.10) * wsh["p_close_side"])
    assert not wpg["better_bookmaker"] and wpg["gain_bookmaker"] == pytest.approx(0)
    assert (wpg["best_exchange_book"], wpg["better_any"]) == ("matchbook", True)
    assert wpg["gain_any"] == pytest.approx((2.00 - 1.90) * wpg["p_close_side"])
    # ANA has no close and isn't valued.
    assert ANA not in rows
