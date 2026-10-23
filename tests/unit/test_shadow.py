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


# Edge bids on every game (#308)

DFO = {
    "season": pl.Int32,
    "game_date": pl.Date,
    "start_utc": pl.Datetime("us", "UTC"),
    "team": pl.String,
    "is_home": pl.Boolean,
    "goalie_name": pl.String,
    "dfo_goalie_id": pl.Int64,
    "status": pl.String,
    "reported_utc": pl.Datetime("us", "UTC"),
    "source_url": pl.String,
    "observed_utc": pl.Datetime("us", "UTC"),
    "raw_key": pl.String,
}


def poll(team: str, observed: datetime, name: str, start: datetime = pf.START) -> dict[str, object]:
    """One Daily Faceoff row: a team's goalie at a poll, for the listing starting at start."""
    return {
        "season": 20262027,
        "game_date": pf.DAY,
        "start_utc": start,
        "team": team,
        "is_home": False,
        "goalie_name": name,
        "dfo_goalie_id": None,
        "status": "Likely",
        "reported_utc": None,
        "source_url": None,
        "observed_utc": observed,
        "raw_key": "k",
    }


def at(hour: int, minute: int = 0, day: int = 7) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def test_every_side_the_policy_didnt_bet_gets_the_lowest_bid_clearing_its_hurdle() -> None:
    decided = ledger()
    bids = shadow.edge_bids(decided)
    # The policy bet every home side, so the away sides are bid, each at its hurdle's cent.
    assert bids.select("game_id", "side").rows() == [(WSH, "away"), (WPG, "away"), (ANA, "away")]
    for row in bids.iter_rows(named=True):
        assert row["p_side"] * row["bid"] - 1 >= row["hurdle"]
        assert row["p_side"] * (row["bid"] - 0.01) - 1 < row["hurdle"]
    assert bids.filter(pl.col("game_id") == WSH)["bid"].item() == 2.12
    # A game the policy didn't bet is bid on both sides.
    unbet = shadow.edge_bids(decided.with_columns(bet=pl.col("game_id") != WSH))
    assert unbet.filter(pl.col("game_id") == WSH)["side"].to_list() == ["away", "home"]


def edge_odds() -> pl.DataFrame:
    """Later Pinnacle snapshots: WSH's away side at its bid only at pre7, after a lower quote,
    and around it quotes that fill nothing (at the decision's own snapshot, stale, at the start);
    WPG's below its bid; ANA's at its bid at pre10."""
    return odds(
        pf.quote(pf.MIDDAY, WSH, "pinnacle", 1.7, 2.5),  # the decision's own snapshot
        pf.quote(at(20), WSH, "pinnacle", 1.85, 2.11, slot="x"),  # below the bid
        pf.quote(at(21), WSH, "pinnacle", 1.7, 2.5, updated=at(20, 50), slot="y"),  # stale
        pf.quote(PRE7 + timedelta(minutes=1), WSH, "pinnacle", 1.8, 2.12, slot="pre7b"),
        pf.quote(at(23, 15), WSH, "pinnacle", 1.7, 2.3, slot="pre8"),
        pf.quote(pf.START, WSH, "pinnacle", 1.6, 2.6, slot="late"),  # at the start
        pf.quote(at(1, 45, day=8), ANA, "pinnacle", 1.95, 1.95, slot="pre10"),
    )


def test_a_bid_fills_at_the_first_later_fresh_quote_at_or_above_it_before_the_start() -> None:
    fills = shadow.edge_fills(shadow.edge_bids(ledger()), edge_odds())
    filled = {row["game_id"]: row for row in fills.iter_rows(named=True) if row["fill_utc"]}
    assert sorted(filled) == [WSH, ANA]
    assert filled[WSH]["fill_utc"] == PRE7 + timedelta(minutes=1)
    assert (filled[WSH]["fill_slot"], filled[WSH]["fill_quote"]) == ("pre7b", 2.12)
    assert filled[ANA]["fill_slot"] == "pre10"
    # WPG's quotes never reach its bid.
    assert fills.filter(pl.col("game_id") == WPG)["fill_utc"].item() is None


def test_a_fill_is_valued_at_its_bid_against_the_same_close() -> None:
    decided, quotes = ledger(), edge_odds()
    filled = shadow.edge_fills(shadow.edge_bids(decided), quotes).filter(
        pl.col("fill_utc").is_not_null()
    )
    valued = shadow.valued(filled.with_columns(price=pl.col("bid")), moves(decided, quotes))
    for row in valued.iter_rows(named=True):
        assert row["clv"] == pytest.approx(row["bid"] * row["p_close_side"] - 1)
        assert row["p_close_side"] == pytest.approx(1 - row["fair_close"])


def test_a_fill_after_a_named_starter_changed_is_flagged() -> None:
    fills = shadow.edge_fills(shadow.edge_bids(ledger()), edge_odds()).filter(
        pl.col("fill_utc").is_not_null()
    )
    polls = pl.DataFrame(
        [
            poll("PIT", at(16), "Tristan Jarry"),
            poll("PIT", at(21), "Alex Nedeljkovic"),
            # A stale listing of another start is not the game's.
            poll("PIT", at(22), "Someone Else", start=pf.START + timedelta(days=2)),
            poll("WSH", at(16), "Charlie Lindgren"),
            poll("WSH", at(21), "Charlie Lindgren"),
            # After the fill, so not read.
            poll("WSH", at(23), "Logan Thompson"),
        ],
        schema=DFO,
    )
    flagged = shadow.starter_changes(fills, polls)
    changed = dict(flagged.select("game_id", "starter_changed").iter_rows())
    # PIT's goalie changed before WSH's fill; ANA's game has no polls, so it is unknown.
    assert changed == {WSH: True, ANA: None}
    same = polls.filter(pl.col("team") == "WSH")
    assert (
        dict(shadow.starter_changes(fills, same).select("game_id", "starter_changed").iter_rows())[
            WSH
        ]
        is False
    )
