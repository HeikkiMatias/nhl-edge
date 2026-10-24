"""Leakage guard for shadow kalshi_prices (#320, ADR 0046): a slot reads only Pinnacle's pair, the
Kalshi book and Daily Faceoff's polls observed by then; Kalshi's prices never reach a fair value;
a maker bid fills only on trades after its posting; the hockey arm's fit reads no game of its own
week; and every figure stays out of the repository (#280). On a SYNTHETIC day."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import shadow_fixtures as sf

from nhl_edge.betting.selection import POLICY_V2
from nhl_edge.live import kalshi_shadow as ks

WEIGHTS = (0.02, 1.05, 0.3)
MIDDAY_ODDS = [("midday", sf.MIDDAY, 1.80, 2.10)]
BOOK_AT = sf.PUBLISHED - timedelta(minutes=1)


def day(books: list, pairs: list | None = None, found: list = (), polls: list = ()):  # type: ignore[assignment]
    return ks.run_day(
        sf.DAY,
        sf.ledger(),
        sf.odds(pairs if pairs is not None else MIDDAY_ODDS),
        sf.depth(books),
        sf.trades(found),
        sf.tops(),
        sf.dailyfaceoff(polls),
        WEIGHTS,
        sf.RULES,
    )


def test_a_book_observed_after_the_slot_is_never_read() -> None:
    later = day([(sf.PUBLISHED + timedelta(seconds=1), sf.HOME, [(0.40, 10.0)], [])])
    assert later.fills.is_empty()
    assert set(later.opportunities["action"].to_list()) == {ks.NO_BOOK}
    logs = ks.markets(
        sf.depth([(sf.PUBLISHED + timedelta(seconds=1), sf.HOME, [(0.40, 10.0)], [])]),
        sf.trades(),
        sf.tops(),
    )
    assert logs[(sf.GAME, sf.HOME)].book(sf.PUBLISHED) is None


def test_a_quote_after_the_slot_never_values_it() -> None:
    def midday(pre7: tuple[float, float]) -> pl.DataFrame:
        pairs = [*MIDDAY_ODDS, ("pre7", sf.PRE7, *pre7)]
        rows = ks.opportunities(sf.ledger(), sf.odds(pairs), sf.dailyfaceoff(), WEIGHTS, sf.DAY)
        return rows.filter(pl.col("slot") == "midday").sort("arm", "team")

    assert midday((1.50, 2.80)).equals(midday((2.80, 1.50)))


def test_no_kalshi_price_reaches_a_fair_value() -> None:
    cheap = day([(BOOK_AT, sf.HOME, [(0.05, 10.0)], [(0.04, 5.0)])])
    dear = day([(BOOK_AT, sf.HOME, [(0.95, 10.0)], [(0.94, 5.0)])])
    columns = ["arm", "slot", "team", "p_side", "hurdle"]
    assert (
        cheap.opportunities.select(columns)
        .sort(columns)
        .equals(dear.opportunities.select(columns).sort(columns))
    )


def test_a_trade_before_a_bid_was_posted_never_fills_it() -> None:
    before = sf.PUBLISHED - timedelta(seconds=1)
    result = day(
        [(BOOK_AT, sf.HOME, [(0.52, 10.0)], [(0.51, 0.0)])],
        found=[(before, sf.HOME, 0.30, 500.0)],
    )
    assert result.fills.filter(pl.col("kind") == ks.MAKER).is_empty()


def test_a_starter_named_after_the_slot_changes_nothing_at_it() -> None:
    polls = [
        (sf.MIDDAY - timedelta(minutes=10), sf.HOME, "Goalie One"),
        (sf.PRE7 + timedelta(seconds=1), sf.HOME, "Goalie Three"),
    ]
    pairs = [*MIDDAY_ODDS, ("pre7", sf.PRE7, 1.80, 2.10)]
    rows = ks.opportunities(sf.ledger(), sf.odds(pairs), sf.dailyfaceoff(polls), WEIGHTS, sf.DAY)
    hockey = rows.filter(pl.col("arm") == ks.HOCKEY)
    assert "pre7" in hockey["slot"].to_list() and not hockey["left"].any()


def test_a_later_snapshot_of_a_slot_never_lends_its_poll_to_an_earlier_one() -> None:
    # Two pre7 snapshots, 5 minutes apart, with a new starter named between them (the audit's
    # probe): the first row reads the polls before it only.
    second = sf.PRE7 + timedelta(minutes=5)
    polls = [
        (sf.MIDDAY - timedelta(minutes=10), sf.HOME, "Goalie One"),
        (sf.PRE7 + timedelta(minutes=2), sf.HOME, "Goalie Three"),
    ]
    pairs = [*MIDDAY_ODDS, ("pre7", sf.PRE7, 1.80, 2.10), ("pre7", second, 1.80, 2.10)]
    rows = ks.opportunities(sf.ledger(), sf.odds(pairs), sf.dailyfaceoff(polls), WEIGHTS, sf.DAY)
    hockey = rows.filter(pl.col("arm") == ks.HOCKEY)
    left = dict(zip(hockey["at_utc"].to_list(), hockey["left"].to_list(), strict=False))
    assert left[sf.PRE7] is False and left[second] is True


def test_the_hockey_fit_reads_no_game_of_its_own_week() -> None:
    # 250 earlier decisions train it; the day's own week's, with results public before the day,
    # are never read, nor an earlier game's result public after the week's fold start.
    target = date(2026, 11, 20)  # a Friday: its week starts on Monday 11-16
    rng = np.random.default_rng(3)

    def rows(days: list[date]) -> tuple[pl.DataFrame, pl.DataFrame]:
        ledger, games = [], []
        for i, on in enumerate(days):
            start = datetime(on.year, on.month, on.day, 23, tzinfo=UTC)
            p_b0 = float(rng.uniform(0.35, 0.65))
            ledger.append(
                {
                    "game_date": on,
                    "season": 20262027,
                    "game_id": 2026020000 + i,
                    "start_utc": start,
                    "home": "BOS",
                    "away": "TOR",
                    "published_utc": start - timedelta(hours=6),
                    "status": "predicted",
                    "policy_version": POLICY_V2,
                    "p_b0": p_b0,
                    "p_b3": p_b0 + 0.02,
                }
            )
            games.append(
                {
                    "game_id": 2026020000 + i,
                    "season": 20262027,
                    "start_utc": start,
                    "home_score": int(rng.uniform() < p_b0) + 2,
                    "away_score": 2,
                    "observed_utc": start + timedelta(hours=11),
                }
            )
        return (
            pl.DataFrame(ledger, schema_overrides={"season": pl.Int32}),
            pl.DataFrame(
                games,
                schema_overrides={
                    "season": pl.Int32,
                    "home_score": pl.Int16,
                    "away_score": pl.Int16,
                },
            ),
        )

    earlier = [date(2026, 10, 19) + timedelta(days=i % 27) for i in range(250)]
    ledger, games = rows(earlier)
    fit = ks.week_fit(ledger, games, target)
    weights = ks.weights_of(fit)
    assert weights is not None
    # its last result was public before the week's fold start, at or before every slot of it
    assert fit["train_cutoff"] < fit["fold_start_utc"]
    own_week = [date(2026, 11, 16) + timedelta(days=i % 4) for i in range(60)]
    extra_ledger, extra_games = rows(own_week)
    extra_ledger = extra_ledger.with_columns(pl.col("game_id") + 10_000)
    extra_games = extra_games.with_columns(
        pl.col("game_id") + 10_000, home_score=pl.lit(0, pl.Int16), away_score=pl.lit(5, pl.Int16)
    )
    with_week = ks.weights_of(
        ks.week_fit(pl.concat([ledger, extra_ledger]), pl.concat([games, extra_games]), target)
    )
    assert with_week == weights


def test_the_shadows_figures_stay_out_of_the_repository() -> None:
    # Its private prefix in R2 only: no committed path, and the module and prefix are listed
    # private, so no model code reads them (test_kalshi_orders.py).
    listed = (Path(__file__).parent / "test_kalshi_orders.py").read_text()
    assert '"kalshi_shadow"' in listed and '"real/shadow"' in listed
    assert ks.PREFIX.startswith("real/shadow/")
    repo = Path(__file__).parents[2]
    assert not (repo / "real").exists() and not list((repo / "reports").rglob("*kalshi_prices*"))
