from datetime import UTC, datetime, timedelta

import polars as pl
import predict_fixtures as pf
import pytest
from pandera.errors import SchemaError

from nhl_edge.betting.selection import POLICY_VERSION
from nhl_edge.live import predict as lp
from nhl_edge.market.devig import fair_probabilities


def decided(**changes: object) -> pl.DataFrame:
    inputs = pf.day(**changes)
    return lp.ledger(lp.decide(inputs), inputs)


def test_every_slate_game_gets_one_row_with_its_prediction() -> None:
    rows = decided()
    assert rows["game_id"].to_list() == list(pf.GAMES)
    assert set(rows["status"]) == {lp.PREDICTED}
    assert set(rows["prediction_utc"]) == {pf.DECISION}
    assert set(rows["decision_snapshot_utc"]) == {pf.MIDDAY}
    assert set(rows["policy_version"]) == {POLICY_VERSION}
    first = rows.row(0, named=True)
    # B0 is Pinnacle's midday price de-vigged, and the blend reads it with B3 and u.
    assert first["p_b0"] == pytest.approx(fair_probabilities([2.10, 1.80])[0])
    live = pf.live()
    u = live.scale.score(
        pl.DataFrame([{"goalie_doubt": 0.3, "availability_doubt": 1.6, "rookie_share": 0.18}])
    )
    assert first["u"] == pytest.approx(u[0])
    expected = live.blends["BLEND"].predict([first["p_b0"]], [0.62], [first["u"]])[0]
    assert first["p_blend"] == pytest.approx(expected)
    assert first["p_b1"] == pytest.approx(live.b1.predict([first["p_b0"]])[0])
    # The best other book is logged beside Pinnacle's, never bet at.
    assert (first["best_home_book"], first["best_home_price"]) == ("bet365", 2.15)
    assert first["price"] in (2.10, 1.80)


def test_bets_follow_the_frozen_policy_and_stakes_stay_in_its_caps() -> None:
    rows = decided()
    bets = rows.filter(pl.col("bet"))
    assert bets.height > 0
    assert (bets["ev"] >= bets["hurdle"]).all()
    assert (bets["fraction"] <= 0.015).all()
    assert bets["fraction"].sum() <= 0.05 + 1e-12
    assert (bets["stake"] == bets["fraction"] * 100.0).all()
    assert rows.filter(~pl.col("bet"))["stake"].to_list() == [0.0] * (rows.height - bets.height)


def test_the_bankroll_counts_earlier_results_public_before_the_decision() -> None:
    earlier = pl.DataFrame(
        {
            "game_id": [1, 2, 3],
            "bet": [True, True, True],
            "side": ["home", "away", "home"],
            "price": [2.0, 1.5, 3.0],
            "stake": [1.0, 2.0, 1.0],
        }
    )
    results = pl.DataFrame(
        {
            "game_id": [1, 2, 3],
            "home_win": [1, 1, 1],
            "result_utc": [
                pf.DECISION - timedelta(hours=7),
                pf.DECISION - timedelta(hours=7),
                pf.DECISION + timedelta(hours=1),
            ],
        },
        schema_overrides={"home_win": pl.Int8},
    )
    # Won 1.0, lost 2.0; the third's result isn't public yet.
    assert lp.bankroll(earlier, results, pf.DECISION) == pytest.approx(99.0)
    assert lp.bankroll(earlier.clear(), results, pf.DECISION) == 100.0


def test_a_stale_quote_gets_no_prediction() -> None:
    stale = pf.MIDDAY - timedelta(minutes=4, seconds=1)  # 6 minutes old at the decision
    quotes = pl.concat(
        [
            pf.day_quotes().filter(
                ~((pl.col("event_id") == "e2026020054") & (pl.col("snapshot_utc") == pf.MIDDAY))
            ),
            pf.quotes(pf.quote(pf.MIDDAY, 2026020054, "pinnacle", 1.9, 2.0, updated=stale)),
        ]
    )
    rows = decided(quotes=quotes)
    status = dict(rows.select("game_id", "status").iter_rows())
    assert status[2026020054] == lp.STALE
    assert status[2026020053] == status[2026020055] == lp.PREDICTED
    assert rows.filter(pl.col("game_id") == 2026020054)["p_blend"].to_list() == [None]
    # Exactly 5 minutes old is still fresh (ADR 0033).
    edge = pf.DECISION - lp.MAX_QUOTE_AGE
    fresh = pl.concat(
        [
            pf.day_quotes().filter(
                ~((pl.col("event_id") == "e2026020054") & (pl.col("snapshot_utc") == pf.MIDDAY))
            ),
            pf.quotes(pf.quote(pf.MIDDAY, 2026020054, "pinnacle", 1.9, 2.0, updated=edge)),
        ]
    )
    assert set(decided(quotes=fresh)["status"]) == {lp.PREDICTED}


def test_a_game_without_a_pinnacle_price_or_an_input_or_already_started_has_no_prediction() -> None:
    quotes = pf.day_quotes().filter(
        ~((pl.col("event_id") == "e2026020055") & (pl.col("book") == "pinnacle"))
    )
    fitted = pf.fitted()
    fitted = lp.Models(
        fitted.b2,
        fitted.b3.filter(pl.col("game_id") != 2026020054),
        fitted.parts,
        fitted.b2_cutoff,
        fitted.b3_cutoff,
    )
    games = {**pf.GAMES, 2026020053: ("WSH", "PIT", pf.DECISION)}
    rows = decided(quotes=quotes, fitted=fitted, slate=pf.slate(games))
    status = dict(rows.select("game_id", "status").iter_rows())
    assert status == {
        2026020053: lp.STARTED,
        2026020054: lp.MISSING,
        2026020055: lp.NO_PRICE,
    }
    assert not rows["bet"].fill_null(False).any()


def test_no_feature_build_means_every_game_lacks_an_input() -> None:
    rows = decided(fitted=None)
    assert set(rows["status"]) == {lp.MISSING}


def test_the_decision_and_its_snapshot_fall_in_the_window() -> None:
    assert lp.in_window(datetime(2026, 10, 7, 16, 45, tzinfo=UTC))
    assert lp.in_window(datetime(2026, 10, 7, 17, 15, tzinfo=UTC))
    assert not lp.in_window(datetime(2026, 10, 7, 17, 15, 1, tzinfo=UTC))
    assert lp.after_window(datetime(2026, 10, 7, 17, 15, 1, tzinfo=UTC))
    # In EST the window is an hour later in UTC.
    assert lp.in_window(datetime(2026, 12, 7, 17, 45, tzinfo=UTC))
    late = decided(decision_utc=datetime(2026, 10, 7, 17, 20, tzinfo=UTC))
    assert set(late["status"]) == {lp.LATE}
    # A snapshot outside the window is no decision snapshot, whatever its label says.
    fallback = pf.day_quotes().with_columns(
        snapshot_utc=pl.when(pl.col("slot") == "midday")
        .then(pl.lit(datetime(2026, 10, 7, 16, 30, tzinfo=UTC)))
        .otherwise(pl.col("snapshot_utc"))
    )
    assert set(decided(quotes=fallback)["status"]) == {lp.NO_SNAPSHOT}


def test_a_later_snapshot_changes_nothing() -> None:
    later = pf.DECISION + timedelta(minutes=1)
    moved = pl.concat(
        [pf.day_quotes(), pf.quotes(pf.quote(later, 2026020053, "pinnacle", 3.0, 1.4))]
    )
    assert decided(quotes=moved).equals(decided())


def test_the_guard_skips_a_bet_the_market_moved_against() -> None:
    base = decided()
    bet = base.filter(pl.col("bet")).row(0, named=True)
    game = bet["game_id"]
    # Move the morning price far in the bet side's favour, so it fell by the decision.
    home, away = (1.3, 3.8) if bet["side"] == "home" else (3.8, 1.3)
    quotes = pl.concat(
        [
            pf.day_quotes().filter(
                ~((pl.col("event_id") == f"e{game}") & (pl.col("slot") == "morning"))
            ),
            pf.quotes(pf.quote(pf.MORNING, game, "pinnacle", home, away, slot="morning")),
        ]
    )
    row = decided(quotes=quotes).filter(pl.col("game_id") == game).row(0, named=True)
    assert row["picked"] and row["guarded"] and not row["bet"]
    assert row["moved_against"] > 0.0535
    assert row["stake"] == 0.0


def test_a_prediction_at_or_after_the_start_is_refused() -> None:
    rows = decided()
    late = rows.with_columns(start_utc=pl.lit(pf.DECISION))
    with pytest.raises(ValueError, match="at or after their game's start"):
        lp.pre_game(late)
    with pytest.raises(SchemaError):
        lp.ledger(lp.decide(pf.day()).with_columns(start_utc=pl.lit(pf.DECISION)), pf.day())


def test_the_feature_build_must_have_finished_before_the_decision_on_this_slate() -> None:
    slate = pf.slate()
    record = pl.DataFrame(
        {
            "table": ["slate", "lineups"],
            "artifact_version": ["live-features-x", "lineup-20261007-abc"],
            "slate_raw_key": [slate["raw_key"][0]] * 2,
            "finished_utc": [datetime(2026, 10, 7, 9, 20, tzinfo=UTC)] * 2,
        }
    )
    rows = {"lineups": pl.DataFrame({"artifact_version": ["lineup-20261007-abc"]})}
    assert lp.build_problems(record, slate, pf.DECISION, rows) == []
    assert lp.build_problems(record.clear(), slate, pf.DECISION, rows) == [
        "no finished feature build for the date"
    ]
    after = record.with_columns(finished_utc=pl.lit(pf.DECISION))
    assert "not before the decision" in lp.build_problems(after, slate, pf.DECISION, rows)[0]
    other = slate.with_columns(raw_key=pl.lit("nhl/schedule/2026-10-07/other"))
    assert lp.build_problems(record, other, pf.DECISION, rows) == [
        "the slate is not the one the feature build rated"
    ]
    mixed = {"lineups": pl.DataFrame({"artifact_version": ["lineup-20261007-abc", "lineup-x"]})}
    assert lp.build_problems(record, slate, pf.DECISION, mixed) == [
        "lineups holds rows of ['lineup-x'], not its build's"
    ]


def test_no_confirmed_starter_is_read() -> None:
    # ADR 0030: live B3 and u read the goalie-start model, never a confirmation, so the tables a
    # run pulls hold no confirmation and the models' inputs can't take one.
    assert not {"pregame_goalies", "dailyfaceoff_goalies"} & set(lp.LAKE_TABLES)


def test_a_day_is_written_to_r2_once() -> None:
    from fakes import ConditionalBucket

    bucket = ConditionalBucket()
    rows = decided()
    assert lp.write_once(bucket, "b", pf.DAY, rows) == "ledger/live/2026-10-07.parquet"
    assert pl.read_parquet(bucket.objects["ledger/live/2026-10-07.parquet"]).equals(rows)
    # A second run, or a late run after a skipped day, can't replace it.
    with pytest.raises(RuntimeError, match="PreconditionFailed"):
        lp.write_once(bucket, "b", pf.DAY, decided(decision_utc=pf.DECISION + timedelta(minutes=1)))
    # A ledger predicting a game at or after its start is never written.
    fresh = ConditionalBucket()
    with pytest.raises(ValueError):
        lp.write_once(fresh, "b", pf.DAY, rows.with_columns(start_utc=pl.lit(pf.DECISION)))
    assert fresh.objects == {}
