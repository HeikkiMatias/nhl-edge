"""Point-in-time rules for shadow kalshi_quotes (#358 step 1, hard rule 1): each fair value at a
moment reads only what was published or observed before it, no quote rests from ten minutes
before the start, a quote fills only on trades after its posting, and the results value the fills
without changing one. On SYNTHETIC logs: the repository holds no Kalshi response (#280)."""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import polars as pl
import quotes_fixtures as qf

from nhl_edge.live import kalshi_quotes as kq
from nhl_edge.live.real import DEMO_EXCHANGE

NOW = qf.START + timedelta(days=1)
TRADE = [(10, qf.HOME, 0.51, 20.0)]


def day(**changes: object) -> kq.DayResult:
    args = {
        "ledger": qf.ledger(),
        "morning": qf.morning(),
        "odds": qf.odds([(qf.PUBLISHED - timedelta(minutes=5), 1.80, 2.10)]),
        "depth": qf.depth(),
        "trades": qf.trades(TRADE),
        "tops": qf.tops(),
        "dailyfaceoff": qf.dailyfaceoff(),
        "games": qf.games(),
    } | changes
    return kq.run_day(qf.DAY, rules=DEMO_EXCHANGE, now=NOW, **args)  # type: ignore[arg-type]


def quoted(result: kq.DayResult) -> pl.DataFrame:
    return result.fills.select("variant", "team", "posted_utc", "fill_utc", "contracts", "price")


def test_a_fair_value_is_known_only_after_its_publication() -> None:
    inputs = kq.inputs_of(
        qf.ledger(), qf.morning(), qf.odds(), qf.depth(), qf.trades(), qf.tops(), qf.dailyfaceoff()
    )
    (game,) = inputs.games
    assert kq.fair_at(kq.MODEL, game, qf.HOME, qf.PUBLISHED, inputs) is None
    after = qf.PUBLISHED + timedelta(microseconds=1)
    assert kq.fair_at(kq.MODEL, game, qf.HOME, after, inputs) == (0.55, qf.PUBLISHED, None)


def test_a_later_decision_or_snapshot_never_changes_an_earlier_quote() -> None:
    before = quoted(day())
    later = pl.concat(
        [
            qf.ledger(published=qf.PUBLISHED - timedelta(hours=1)),
            qf.ledger(p_blend=0.70, published=qf.at(30)),
        ]
    )
    pairs = [(qf.PUBLISHED - timedelta(minutes=5), 1.80, 2.10), (qf.at(30), 1.40, 3.00)]
    # The morning view stands in for the first publication, so the decision is the later one.
    moved = quoted(
        day(
            ledger=later.tail(1),
            morning=qf.morning([(0.55, qf.PUBLISHED)]),
            odds=qf.odds(pairs),
        )
    )
    early = pl.col("fill_utc") < qf.at(30)
    assert moved.filter(early).sort(pl.all()).equals(before.filter(early).sort(pl.all()))


def test_a_top_observed_after_the_moment_is_never_read() -> None:
    series = kq.tops_of(qf.tops())[(qf.GAME, qf.HOME)]
    found = kq.top_at(series, qf.at(3) - timedelta(microseconds=1))
    assert found is not None and found[0] == qf.at(2)
    assert kq.top_at(series, qf.at(3))[0] == qf.at(3)  # type: ignore[index]


def test_a_later_top_never_changes_an_earlier_quote_or_pull() -> None:
    # The anchored arm reads the mid, and a move pulls: a move at minute 30 changes nothing before.
    trades = qf.trades([(10, qf.HOME, 0.51, 20.0), (35, qf.HOME, 0.40, 20.0)])
    before = day(trades=trades)
    moved = day(trades=trades, tops=qf.tops(moved={30: (0.40, 0.44)}))
    early = pl.col("fill_utc") < qf.at(30)
    assert before.fills.filter(early, pl.col("fair") == kq.ANCHORED).height
    assert (
        quoted(moved)
        .filter(early)
        .sort(pl.all())
        .equals(quoted(before).filter(early).sort(pl.all()))
    )


def test_a_later_daily_faceoff_poll_never_changes_an_earlier_quote() -> None:
    first = [(qf.at(-60), qf.HOME, "Logan Thompson")]
    before = quoted(day(dailyfaceoff=qf.dailyfaceoff(first)))
    changed = [*first, (qf.at(20), qf.HOME, "Charlie Lindgren")]
    after = quoted(day(dailyfaceoff=qf.dailyfaceoff(changed)))
    early = pl.col("fill_utc") < qf.at(20)
    assert before.filter(early).height
    assert after.filter(early).sort(pl.all()).equals(before.filter(early).sort(pl.all()))


def test_a_later_nearer_listing_never_undoes_an_earlier_starter_change() -> None:
    # Two polls of a listing 5 minutes off the start name different goalies; a later poll of the
    # exact start's listing, nearer the game, must not remove that change after the fact.
    off = qf.START + timedelta(minutes=5)
    early = qf.dailyfaceoff(
        [(qf.at(-60), qf.HOME, "Logan Thompson"), (qf.at(5), qf.HOME, "Charlie Lindgren")]
    ).with_columns(start_utc=pl.lit(off).cast(qf.UTC_TYPE))
    later = pl.concat([early, qf.dailyfaceoff([(qf.at(30), qf.HOME, "Logan Thompson")])])
    games = kq.slate_of(qf.ledger())
    assert kq.change_times(games, early)[qf.GAME] == [qf.at(5)]
    assert kq.change_times(games, later)[qf.GAME] == [qf.at(5), qf.at(30)]
    trades = qf.trades([(10, qf.HOME, 0.51, 20.0), (35, qf.HOME, 0.51, 20.0)])
    before, after = day(trades=trades, dailyfaceoff=early), day(trades=trades, dailyfaceoff=later)
    assert before.counts["model-2c"]["pulled_starter"] == 1
    upto = pl.col("fill_utc") < qf.at(30)
    assert (
        quoted(after).filter(upto).sort(pl.all()).equals(quoted(before).filter(upto).sort(pl.all()))
    )


def test_no_day_it_reads_informed_its_rules() -> None:
    # The half-spreads were chosen from the watch's tops of 2026-10-28 to 10-30.
    assert kq.FIRST_DAY > kq.RULES_FIXED == date(2026, 10, 30)
    assert kq.due(kq.RULES_FIXED + timedelta(days=1), []) == []


def test_no_quote_rests_from_ten_minutes_before_the_start() -> None:
    start = qf.at(25)
    trades = qf.trades([(m, qf.HOME, 0.51, 5.0) for m in range(1, 25)])
    fills = day(ledger=qf.ledger(start=start), trades=trades).fills
    assert fills.height and (fills["fill_utc"] <= start - kq.STOP_BEFORE).all()
    assert (fills["posted_utc"] < start - kq.STOP_BEFORE).all()


def test_a_quote_fills_only_on_trades_after_its_posting() -> None:
    # A trade at the first top's own instant comes before the first quote, posted just after it.
    at_post = day(trades=qf.trades([(0, qf.HOME, 0.51, 100.0)]))
    assert at_post.fills.is_empty()
    fills = day().fills
    assert fills.height and (fills["fill_utc"] > fills["posted_utc"]).all()


def test_the_result_values_a_fill_and_never_changes_one() -> None:
    won, lost = day(games=qf.games(True)), day(games=qf.games(False))
    assert quoted(won).equals(quoted(lost))
    assert set(won.fills["won"].to_list()) != set(lost.fills["won"].to_list())


def test_the_half_spreads_and_the_caps_are_fixed_before_any_reading() -> None:
    # #358's rules, set before the first run: changing one is a new version.
    assert kq.VERSION == "1"
    assert (Decimal("0"), Decimal("0.01"), Decimal("0.02")) == kq.HALF_SPREADS
    assert (Decimal(10), Decimal(30), Decimal(100)) == (kq.SIZE, kq.NET_CAP, kq.DAY_CAP)
    assert (
        timedelta(minutes=10),
        Decimal("0.03"),
        0.5,
    ) == (kq.STOP_BEFORE, kq.PULL_MOVE, kq.ANCHOR_WEIGHT)


def test_the_quotes_figures_stay_out_of_the_repository() -> None:
    # Its private prefix in R2 only: no committed path, and the module and prefix are listed
    # private, so no model code reads them (test_kalshi_orders.py).
    listed = (Path(__file__).parent / "test_kalshi_orders.py").read_text()
    assert '"kalshi_quotes"' in listed and '"real/shadow"' in listed
    assert kq.PREFIX.startswith("real/shadow/")
    repo = Path(__file__).parents[2]
    assert not (repo / "real").exists() and not list((repo / "reports").rglob("*kalshi_quotes*"))
