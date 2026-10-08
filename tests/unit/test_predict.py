from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

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
    # A game played two days after its bet's start was postponed: its bet is void (#165).
    start = pf.DECISION - timedelta(days=3)
    played = results.with_columns(played_utc=pl.lit(start)).with_columns(
        played_utc=pl.when(pl.col("game_id") == 2)
        .then(pl.col("played_utc") + timedelta(days=2))
        .otherwise(pl.col("played_utc"))
    )
    starts = earlier.with_columns(start_utc=pl.lit(start))
    assert lp.bankroll(starts, played, pf.DECISION) == pytest.approx(101.0)


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


def test_a_game_starting_before_the_decision_is_published_has_no_prediction() -> None:
    # A matinee at 12:55 ET, decided at 12:47 ET and published at 12:56 (#170): no prediction.
    # The other games are decided as if it were not there.
    matinee = datetime(2026, 10, 7, 16, 55, tzinfo=UTC)
    games = {**pf.GAMES, 2026020053: ("WSH", "PIT", matinee)}
    on_time = decided(slate=pf.slate(games), published_utc=matinee - timedelta(minutes=1))
    late = decided(slate=pf.slate(games), published_utc=matinee + timedelta(minutes=1))
    status = dict(late.select("game_id", "status").iter_rows())
    assert dict(on_time.select("game_id", "status").iter_rows())[2026020053] == lp.PREDICTED
    assert status == {
        2026020053: lp.STARTS_BEFORE_PUBLISHED,
        2026020054: lp.PREDICTED,
        2026020055: lp.PREDICTED,
    }
    assert late.filter(pl.col("game_id") == 2026020053)["p_blend"].to_list() == [None]
    assert set(late["published_utc"]) == {matinee + timedelta(minutes=1)}
    assert set(late["prediction_utc"]) == {pf.DECISION}
    # The odds' own start counts too, as for a game under way at the decision.
    quotes = pf.day_quotes().with_columns(
        commence_time_utc=pl.when(pl.col("event_id") == "e2026020054")
        .then(pl.lit(matinee))
        .otherwise(pl.col("commence_time_utc"))
    )
    moved = decided(quotes=quotes, published_utc=matinee)
    assert dict(moved.select("game_id", "status").iter_rows())[2026020054] == (
        lp.STARTS_BEFORE_PUBLISHED
    )
    # Unset, the publication is the decision instant; never before it.
    assert set(decided()["published_utc"]) == {pf.DECISION}
    with pytest.raises(ValueError, match="before the decision"):
        decided(published_utc=pf.DECISION - timedelta(seconds=1))


def test_the_ledger_is_published_after_the_decision_and_before_each_start() -> None:
    rows = decided()
    with pytest.raises(SchemaError):
        lp.ledger(
            lp.decide(pf.day()).with_columns(published_utc=pl.lit(pf.DECISION - timedelta(1))),
            pf.day(),
        )
    published_late = rows.with_columns(published_utc=pl.lit(pf.START))
    with pytest.raises(ValueError, match="at or after their game's start"):
        lp.pre_game(published_late)
    # By the clock at the write, too: a start, or a quote aged past the limit.
    inputs = pf.day()
    limit = pf.MIDDAY + lp.MAX_PUBLISHED_AGE
    assert lp.outdated(rows, inputs, limit).is_empty()
    assert lp.outdated(rows, inputs, limit + timedelta(seconds=1)).height == rows.height
    fresh = rows.with_columns(last_update_utc=pl.lit(pf.START - timedelta(minutes=1)))
    assert lp.outdated(fresh, inputs, pf.START - timedelta(seconds=1)).is_empty()
    assert set(lp.outdated(fresh, inputs, pf.START)["game_id"]) == {2026020053, 2026020054}


def clock(*times: datetime) -> Any:
    """A clock reading the given times in turn, then the last one."""
    readings = iter(times)
    last: list[datetime] = []

    def read() -> datetime:
        last[:] = [next(readings, last[0] if last else times[-1])]
        return last[0]

    return read


def test_a_game_starting_while_the_ledger_is_built_is_decided_again_as_started() -> None:
    # Codex's P1 on #184: the matinee starts at 13:00 ET between the publication clock and the
    # check after the ledger is built. The day is decided again at the later clock: the matinee
    # has no prediction, and the other games keep theirs.
    matinee = datetime(2026, 10, 7, 17, tzinfo=UTC)
    games = {**pf.GAMES, 2026020053: ("WSH", "PIT", matinee)}
    inputs = pf.day(slate=pf.slate(games))
    second = timedelta(seconds=1)
    rows = lp.publish(inputs, clock(matinee - second, matinee + second, matinee + 2 * second))
    status = dict(rows.select("game_id", "status").iter_rows())
    assert status[2026020053] == lp.STARTS_BEFORE_PUBLISHED
    assert status[2026020054] == status[2026020055] == lp.PREDICTED
    # Stamped with the clock that last checked it (Codex's P1 on #184).
    assert set(rows["published_utc"]) == {matinee + 2 * second}
    # Nothing starts while it is built: one pass, stamped at the check after it.
    once = lp.publish(inputs, clock(matinee - 2 * second, matinee - second))
    assert set(once["published_utc"]) == {matinee - second}
    assert dict(once.select("game_id", "status").iter_rows())[2026020053] == lp.PREDICTED


def test_a_quote_older_than_the_limit_at_publication_is_no_fresh_price() -> None:
    # ADR 0033's amendment: the quote is at most 15 minutes old at publication.
    limit = pf.MIDDAY + lp.MAX_PUBLISHED_AGE
    on_time = decided(published_utc=limit)
    assert set(on_time["status"]) == {lp.PREDICTED}
    stalled = decided(published_utc=limit + timedelta(seconds=1))
    assert set(stalled["status"]) == {lp.STALE}
    assert not stalled["bet"].fill_null(False).any()
    # A run that ages past the limit while it builds is decided again at the later clock.
    times = clock(limit - timedelta(seconds=1), limit + timedelta(seconds=1), limit)
    assert set(lp.publish(pf.day(), times)["status"]) == {lp.STALE}


def test_the_odds_start_counts_when_the_day_is_checked_again() -> None:
    # Codex's P1 on #184: the odds put a game's start earlier than the slate. It crosses that
    # start while the ledger is built, so the day is decided again with it started.
    early = datetime(2026, 10, 7, 17, tzinfo=UTC)
    quotes = pf.day_quotes().with_columns(
        commence_time_utc=pl.when(pl.col("event_id") == "e2026020054")
        .then(pl.lit(early))
        .otherwise(pl.col("commence_time_utc"))
    )
    inputs = pf.day(quotes=quotes)
    second = timedelta(seconds=1)
    rows = lp.publish(inputs, clock(early - second, early + second, early + 2 * second))
    status = dict(rows.select("game_id", "status").iter_rows())
    assert status[2026020054] == lp.STARTS_BEFORE_PUBLISHED
    assert status[2026020053] == status[2026020055] == lp.PREDICTED


def test_a_game_starting_before_the_write_has_the_day_decided_again() -> None:
    from fakes import ConditionalBucket

    matinee = datetime(2026, 10, 7, 17, tzinfo=UTC)
    games = {**pf.GAMES, 2026020053: ("WSH", "PIT", matinee)}
    inputs = pf.day(slate=pf.slate(games))
    second = timedelta(seconds=1)
    bucket = ConditionalBucket()
    # Built before the start, but the clock at the write is past it: decided again, then written.
    times = clock(matinee - 2 * second, matinee - second, matinee, matinee + second, matinee)
    rows, key = lp.write_published(bucket, "b", inputs, times)
    assert key == "ledger/live/2026-10-07.parquet"
    written = pl.read_parquet(bucket.objects[key])
    assert written.equals(rows)
    assert dict(written.select("game_id", "status").iter_rows())[2026020053] == (
        lp.STARTS_BEFORE_PUBLISHED
    )


def test_the_feature_build_must_have_finished_before_the_decision_on_this_slate() -> None:
    slate = pf.slate()
    record = pl.DataFrame(
        {
            "table": ["slate", "lineups"],
            "artifact_version": ["live-features-x", "lineup-20261007-abc"],
            "slate_raw_key": [slate["raw_key"][0]] * 2,
            "finished_utc": [datetime(2026, 10, 7, 9, 20, tzinfo=UTC)] * 2,
            "build_id": ["live-features-20261007-abc1234"] * 2,
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
    # A build of uncommitted code: refused for a logged decision, read by a dry run.
    local = record.with_columns(build_id=pl.lit("live-features-20261007-abc1234-dirty"))
    assert lp.build_problems(local, slate, pf.DECISION, rows) == [
        "the feature build live-features-20261007-abc1234-dirty ran uncommitted code"
    ]
    assert lp.build_problems(local, slate, pf.DECISION, rows, committed=False) == []


def test_no_confirmed_starter_is_read() -> None:
    # ADR 0030: live B3 and u read the goalie-start model, never a confirmation, so the tables a
    # run pulls hold no confirmation and the models' inputs can't take one.
    assert not {"pregame_goalies", "dailyfaceoff_goalies"} & set(lp.LAKE_TABLES)


def test_a_day_is_written_to_r2_once() -> None:
    from fakes import ConditionalBucket

    bucket = ConditionalBucket()
    inputs = pf.day()
    rows = decided()
    put = pf.DECISION + timedelta(minutes=8)
    now = clock(put)
    key, written = lp.write_once(bucket, "b", inputs, rows, now)
    assert key == "ledger/live/2026-10-07.parquet"
    # Stamped with the clock read at the write, its publication.
    assert set(written["published_utc"]) == {put}
    assert written.drop("published_utc").equals(rows.drop("published_utc"))
    assert pl.read_parquet(bucket.objects[key]).equals(written)
    # A second run, or a late run after a skipped day, can't replace it.
    later = pf.day(decision_utc=pf.DECISION + timedelta(minutes=1))
    with pytest.raises(RuntimeError, match="PreconditionFailed"):
        lp.write_once(bucket, "b", later, lp.ledger(lp.decide(later), later), now)
    # A ledger predicting a game at or after its start is never written.
    fresh = ConditionalBucket()
    with pytest.raises(ValueError):
        lp.write_once(fresh, "b", inputs, rows.with_columns(start_utc=pl.lit(pf.DECISION)), now)
    # Nor one whose game has started by the clock at the put, read after serializing.
    with pytest.raises(lp.Republish):
        lp.write_once(fresh, "b", inputs, rows, clock(pf.START))
    assert fresh.objects == {}


def test_a_refused_price_pair_costs_only_its_game() -> None:
    # Both sides at plus money sum below 100%: de-vigging refuses it, for that game alone.
    quotes = pl.concat(
        [
            pf.day_quotes().filter(
                ~((pl.col("event_id") == "e2026020054") & (pl.col("snapshot_utc") == pf.MIDDAY))
            ),
            pf.quotes(pf.quote(pf.MIDDAY, 2026020054, "pinnacle", 2.1, 2.1)),
        ]
    )
    status = dict(decided(quotes=quotes).select("game_id", "status").iter_rows())
    assert status == {
        2026020053: lp.PREDICTED,
        2026020054: lp.NO_PRICE,
        2026020055: lp.PREDICTED,
    }


def odds_body(home: str, away: str, start: str, prices: tuple[float, float]) -> bytes:
    import json

    market = {
        "key": "h2h",
        "last_update": "2026-10-07T16:45:10Z",
        "outcomes": [{"name": home, "price": prices[0]}, {"name": away, "price": prices[1]}],
    }
    event = {
        "id": "e1",
        "home_team": home,
        "away_team": away,
        "commence_time": start,
        "bookmakers": [
            {"key": "pinnacle", "last_update": "2026-10-07T16:45:10Z", "markets": [market]}
        ],
    }
    return json.dumps([event]).encode()


def test_a_malformed_snapshot_is_left_out_and_reported(tmp_path: Any) -> None:
    from nhl_edge.lake.raw import RawStore

    store = RawStore(tmp_path)
    good = odds_body(
        "Washington Capitals", "Pittsburgh Penguins", "2026-10-07T23:30:00Z", (2.1, 1.8)
    )
    store.put(
        "odds",
        "2026-10-07/20261007T164530Z_midday_eu",
        good,
        {"fetched_utc": "2026-10-07T16:45:30+00:00", "slot": "midday"},
    )
    store.put(
        "odds",
        "2026-10-07/20261007T110600Z_morning_eu",
        b"not json",
        {"fetched_utc": "2026-10-07T11:06:00+00:00", "slot": "morning"},
    )
    quotes, failed = lp.day_quotes(store, pf.DAY)
    assert set(quotes["slot"]) == {"midday"}
    assert quotes.height == 2
    assert len(failed) == 1 and failed[0].startswith("odds/2026-10-07/20261007T110600Z_morning_eu")


def test_the_ledgers_in_r2_are_read_back_whole() -> None:
    import io

    from fakes import MemoryBucket

    bucket = MemoryBucket(page_size=1)
    first = decided()
    second = decided(slate=pf.slate({2026020099: ("BOS", "TOR", pf.START)}), quotes=pf.day_quotes())
    for day, frame in (
        ("2026-10-07", first),
        ("2026-10-08", second.with_columns(game_date=pl.lit(pf.DAY + timedelta(days=1)))),
    ):
        body = io.BytesIO()
        frame.write_parquet(body)
        bucket.objects[f"ledger/live/{day}.parquet"] = body.getvalue()
    synced = lp.sync_ledgers(bucket, "b", 20262027)
    assert synced.height == first.height + second.height
    assert sorted(set(synced["game_date"].to_list())) == [pf.DAY, pf.DAY + timedelta(days=1)]
    assert lp.sync_ledgers(MemoryBucket(), "b", 20262027).is_empty()


def test_a_real_run_decides_today_from_the_committed_fit(monkeypatch: Any) -> None:
    from typer.testing import CliRunner

    from nhl_edge import cli
    from nhl_edge.backtest import reports

    monkeypatch.setattr(reports, "version", lambda component, now: f"{component}-20261007-abc1234")
    runner = CliRunner()
    other_day = runner.invoke(cli.app, ["predict", "--r2", "--date", "2000-01-01"])
    assert other_day.exit_code == 2
    assert "today's slate only" in " ".join(other_day.output.split())
    other_fit = runner.invoke(cli.app, ["predict", "--r2", "--fit", "x.json"])
    assert other_fit.exit_code == 2


def test_model_inputs_are_cut_at_the_decision_snapshot() -> None:
    # The price bet was observed at the snapshot, so nothing learned after it may inform the bet.
    assert lp.input_cutoff(pf.day_quotes(), pf.DECISION) == pf.MIDDAY < pf.DECISION
    # Without a snapshot in the window the day is skipped, and the decision instant stands.
    morning = pf.day_quotes().filter(pl.col("slot") == "morning")
    assert lp.input_cutoff(morning, pf.DECISION) == pf.DECISION


def test_a_season_of_ledgers_validates_one_decision_per_date() -> None:
    from nhl_edge.lake.schemas import PaperLedger

    first = decided()
    tomorrow = decided(decision_utc=pf.DECISION + timedelta(minutes=1)).with_columns(
        game_date=pl.lit(pf.DAY + timedelta(days=1))
    )
    # Two dates, two decision instants, and a postponed game decided again on its new date.
    PaperLedger.validate(pl.concat([first, tomorrow]))
    with pytest.raises(SchemaError):
        PaperLedger.validate(
            pl.concat([first, tomorrow.with_columns(game_date=pl.lit(pf.DAY))]).unique(
                ["game_date", "game_id", "prediction_utc"]
            )
        )


def test_the_published_ledger_is_the_day_decided_at_its_stamp() -> None:
    # Every row a later clock would change counts, not only predictions (Codex on #187): a
    # missing-input row whose quote ages past the limit is decided again as stale, so the
    # ledger stamped with that clock is the day decided at it.
    limit = pf.MIDDAY + lp.MAX_PUBLISHED_AGE
    inputs = pf.day(fitted=None)
    second = timedelta(seconds=1)
    rows = lp.publish(inputs, clock(limit - second, limit + second, limit + 2 * second))
    assert set(rows["status"]) == {lp.STALE}
    stamp = rows["published_utc"][0]
    again = replace(inputs, published_utc=stamp)
    assert lp.ledger(lp.decide(again), again).equals(rows)
    # A skipped day changes with no clock: published in one pass.
    late = pf.day(decision_utc=datetime(2026, 10, 7, 17, 20, tzinfo=UTC))
    assert set(lp.publish(late, clock(late.decision_utc))["status"]) == {lp.LATE}
