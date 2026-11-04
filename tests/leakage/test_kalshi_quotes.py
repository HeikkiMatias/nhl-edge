"""Point-in-time rules for shadow kalshi_quotes (#358 step 1, hard rule 1): each fair value at a
moment reads only what was published or observed before it, no quote rests from ten minutes
before the start, a quote fills only on trades after its posting, and the results value the fills
without changing one. On SYNTHETIC logs: the repository holds no Kalshi response (#280)."""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import polars as pl
import quotes_fixtures as qf

from nhl_edge.live import close_forecast as cf
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


def test_a_close_forecast_is_known_only_after_its_publication() -> None:
    forecast = pl.DataFrame(
        {
            "game_date": [kq.FORECAST_FIRST_DAY],
            "game_id": [qf.GAME],
            "status": ["predicted"],
            "p_close_hat": [0.53],
            "published_utc": [qf.PUBLISHED],
            "forecast_version": [cf.VERSION],
        }
    ).with_columns(pl.col("published_utc").cast(qf.UTC_TYPE))
    inputs = kq.inputs_of(
        qf.ledger(),
        qf.morning(),
        qf.odds(),
        qf.depth(),
        qf.trades(),
        qf.tops(),
        qf.dailyfaceoff(),
        forecast,
    )
    (game,) = inputs.games
    assert kq.fair_at(kq.FORECAST, game, qf.HOME, qf.PUBLISHED, inputs) is None
    after = qf.PUBLISHED + timedelta(microseconds=1)
    assert kq.fair_at(kq.FORECAST, game, qf.HOME, after, inputs) == (0.53, qf.PUBLISHED, None)
    # Its first day comes after its registration (ADR 0054): no day it reads informed it. So
    # does version 2's (ADR 0056).
    assert kq.FORECAST_FIRST_DAY > cf.REGISTERED_ON
    assert kq.FORECAST2_FIRST_DAY > cf.REGISTERED_ON_2


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


# The hold rules after a move (#385).


def held_day(**changes: object) -> kq.DayResult:
    args = {
        "ledger": qf.ledger(),
        "morning": qf.morning(),
        "odds": qf.odds(),
        "depth": qf.depth(),
        "trades": qf.trades(TRADE),
        "tops": qf.tops(),
        "dailyfaceoff": qf.dailyfaceoff(),
        "games": qf.games(),
    } | changes
    return kq.run_day(qf.DAY, rules=DEMO_EXCHANGE, now=NOW, holds=True, **args)  # type: ignore[arg-type]


def test_return_never_reads_the_mid_before_it_is_observed() -> None:
    # The mid comes back at minute 12: a trade at 11.9 finds return still holding.
    tops = qf.tops(moved={7: (0.54, 0.58), 12: (0.50, 0.54)})
    trades = qf.trades([(11.9, qf.HOME, 0.51, 20.0), (12.5, qf.HOME, 0.51, 20.0)])
    fills = held_day(tops=tops, trades=trades).fills.filter(pl.col("variant") == "model-2c-return")
    assert fills["fill_utc"].to_list() == [qf.at(12.5)]
    assert (fills["posted_utc"] > qf.at(12)).all()


def test_a_later_top_never_changes_an_earlier_hold() -> None:
    trades = qf.trades([(10, qf.HOME, 0.51, 20.0), (35, qf.HOME, 0.40, 20.0)])
    moved = qf.tops(moved={7: (0.54, 0.58)})
    before = held_day(trades=trades, tops=moved)
    after = held_day(trades=trades, tops=qf.tops(moved={7: (0.54, 0.58), 30: (0.50, 0.54)}))
    early = pl.col("fill_utc") < qf.at(30)
    assert quoted(before).filter(early).height
    assert (
        quoted(after)
        .filter(early)
        .sort(pl.all())
        .equals(quoted(before).filter(early).sort(pl.all()))
    )


def test_cool_never_ends_a_hold_before_its_fifteen_minutes() -> None:
    trades = qf.trades([(21.9, qf.HOME, 0.51, 20.0)])
    result = held_day(tops=qf.tops(moved={7: (0.54, 0.58)}), trades=trades)
    assert result.fills.filter(pl.col("variant") == "model-2c-cool").is_empty()


def test_the_hold_rules_are_fixed_before_any_figure_under_them() -> None:
    # #385's rules, fixed on the issue before any figure under them was read: changing one is a
    # new version of the hold rules' record.
    assert kq.HOLDS_VERSION == "1" and date(2026, 11, 3) == kq.HOLDS_FIXED
    assert kq.HOLDS == ("wait", "cool", "return", "trust")
    assert (
        timedelta(minutes=15),
        timedelta(hours=3),
        Decimal("0.01"),
    ) == (kq.COOL_OFF, kq.LATE, kq.PRIMARY_HALF_SPREAD)


def test_the_hold_figures_stay_out_of_the_repository() -> None:
    assert kq.HOLDS_PREFIX.startswith("real/shadow/kalshi_quotes/")
    assert kq.HOLDS_PREFIX != kq.PREFIX and not kq.PREFIX.startswith(kq.HOLDS_PREFIX)
    repo = Path(__file__).parents[2]
    assert not list((repo / "reports").rglob("*holds*"))


def test_no_day_that_led_to_the_hold_rules_is_scored() -> None:
    # The demo of 2026-11-02 led to #385's rules, fixed on 11-03: they are scored from 11-04.
    assert kq.HOLDS_FIRST_DAY > kq.HOLDS_FIXED > date(2026, 11, 2)
    assert kq.due(kq.HOLDS_FIXED + timedelta(days=1), [], kq.HOLDS_FIRST_DAY) == []
    assert kq.due(kq.HOLDS_FIRST_DAY + timedelta(days=2), [], kq.HOLDS_FIRST_DAY) == [
        kq.HOLDS_FIRST_DAY,
        kq.HOLDS_FIRST_DAY + timedelta(days=1),
    ]


def test_the_rounding_fix_never_changes_a_day_kept_before_it() -> None:
    # #376 counts from a stated game date after its go-ahead (2026-11-04), so every kept day
    # before it replays with the floor it was kept with.
    assert date(2026, 11, 5) == kq.ROUNDED_FROM and kq.FAIR_PLACES == 10
    trades = qf.trades([(10, qf.AWAY, 0.44, 20.0), (10, qf.HOME, 0.51, 20.0)])
    kept = day(trades=trades)
    assert qf.DAY < kq.ROUNDED_FROM
    # At a cent's half-spread the old floor bids 0.43 for the away side, below the trade at 0.44.
    away = kept.fills.filter(pl.col("team") == qf.AWAY, pl.col("variant") == "model-1c")
    assert away.is_empty()
