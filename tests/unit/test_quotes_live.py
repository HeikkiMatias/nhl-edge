"""The dashboard's live view of market making (#398 step 3) on SYNTHETIC logs: the simulator's
day up to now, each game and team's state, our bid and holding, the same as the nightly record at
the day's end, the morning view's slate before the decision, the watch's polls read once each,
and the command with counts only in its log."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import quotes_fixtures as qf

from nhl_edge.live import kalshi_quotes as kq
from nhl_edge.live import quotes_live as ql
from nhl_edge.live.real import DEMO_EXCHANGE

RAW = {"ticker", "trade_id", "order_id", "fill_utc", "posted_utc"}


def inputs(ledger: pl.DataFrame | None = None) -> kq.Inputs:
    return kq.inputs_of(
        qf.ledger() if ledger is None else ledger,
        qf.morning(),
        qf.odds(),
        qf.depth(),
        qf.trades([(10, qf.HOME, 0.51, 20.0)]),
        qf.tops(),
        qf.dailyfaceoff(),
    )


def row(rows: pl.DataFrame, variant: str, team: str) -> dict[str, Any]:
    return rows.filter(pl.col("variant") == variant, pl.col("team") == team).row(0, named=True)


def test_each_game_and_team_as_the_day_stands_now() -> None:
    rows = ql.live_rows(inputs(), DEMO_EXCHANGE, qf.at(5), "quotes-live-x")
    assert rows.schema == pl.Schema(ql.LIVE_SCHEMA)
    # One game, both teams, our model at each half-spread.
    assert rows.height == 2 * len(ql.LIVE)
    home = row(rows, "model-2c", qf.HOME)
    # The fair value 0.55 less 2 cents, below the best ask of 0.54.
    assert (home["state"], home["bid_price"], home["bid_left"]) == (ql.QUOTING, 0.53, 10.0)
    assert home["fair"] == pytest.approx(0.55) and (home["yes_bid"], home["yes_ask"]) == (
        0.50,
        0.54,
    )
    assert home["top_utc"] == qf.at(5) and home["contracts"] == 0.0 and home["fills"] == 0
    assert (home["home"], home["opponent"], home["half_spread_cents"]) == (True, qf.AWAY, 2)
    away = row(rows, "model-2c", qf.AWAY)
    assert away["fair"] == pytest.approx(0.45) and away["state"] == ql.QUOTING
    assert not RAW & set(rows.columns)


def test_a_fill_shows_as_held_at_its_cost() -> None:
    rows = ql.live_rows(inputs(), DEMO_EXCHANGE, qf.at(15), "quotes-live-x")
    home = row(rows, "model-2c", qf.HOME)
    # The trade at minute 10 filled the 10 contracts bid at 0.53, with the maker fee.
    assert (home["contracts"], home["fills"]) == (10.0, 1)
    fill = kq.run_variant(kq.MODEL, Decimal("0.02"), inputs(), DEMO_EXCHANGE).fills[0]
    assert home["cost_usd"] == pytest.approx(float(fill["cost_usd"]))
    # Nothing after now is known: at minute 9 the trade hasn't come.
    early = row(ql.live_rows(inputs(), DEMO_EXCHANGE, qf.at(9), "x"), "model-2c", qf.HOME)
    assert early["contracts"] == 0.0


def test_the_states_before_the_fair_value_at_the_stop_and_after_the_start() -> None:
    before = ql.live_rows(inputs(), DEMO_EXCHANGE, qf.PUBLISHED - timedelta(minutes=1), "x")
    assert set(before["state"]) == {ql.WAITING} and before["fair"].is_null().all()
    # The tops end at minute 40. A bid rests until a step replaces or cancels it, as in the
    # record: with no step since, the last one's bids still rest at minute 60.
    stale = ql.live_rows(inputs(), DEMO_EXCHANGE, qf.at(60), "x")
    assert set(stale["state"]) == {ql.QUOTING} and stale["bid_price"].is_not_null().all()
    stopped = ql.live_rows(inputs(), DEMO_EXCHANGE, qf.START - timedelta(minutes=5), "x")
    assert set(stopped["state"]) == {ql.STOPPED}
    started = ql.live_rows(inputs(), DEMO_EXCHANGE, qf.START + timedelta(minutes=1), "x")
    assert set(started["state"]) == {ql.STARTED}


def test_a_pulled_game_says_why() -> None:
    # The home market's mid moves 4 cents after the bids went up: the game is pulled on the move.
    moved = kq.inputs_of(
        qf.ledger(),
        qf.morning(),
        qf.odds(),
        qf.depth(),
        qf.trades(),
        qf.tops(moved={7: (0.54, 0.58)}),
        qf.dailyfaceoff(),
    )
    rows = ql.live_rows(moved, DEMO_EXCHANGE, qf.at(9), "x")
    assert set(rows["state"]) == {ql.PULLED_MOVE} and rows["bid_price"].is_null().all()


def test_at_the_days_end_the_live_view_holds_what_the_record_kept() -> None:
    result = kq.run_day(
        qf.DAY,
        qf.ledger(),
        qf.morning(),
        qf.odds(),
        qf.depth(),
        qf.trades([(10, qf.HOME, 0.51, 20.0)]),
        qf.tops(),
        qf.dailyfaceoff(),
        qf.games(),
        DEMO_EXCHANGE,
        qf.START + timedelta(days=1),
    )
    rows = ql.live_rows(inputs(), DEMO_EXCHANGE, qf.START + timedelta(minutes=1), "x")
    for fair, h in ql.LIVE:
        name = kq.variant_name(fair, h)
        kept = result.fills.filter(pl.col("variant") == name)
        live = rows.filter(pl.col("variant") == name)
        assert live["contracts"].sum() == pytest.approx(kept["contracts"].sum())
        assert live["cost_usd"].sum() == pytest.approx(kept["cost_usd"].sum())
        assert live["fills"].sum() == kept.height


def lake_tables(ledger: pl.DataFrame, morning: pl.DataFrame) -> dict[str, pl.DataFrame]:
    return {
        "paper_ledger": ledger,
        "morning_predictions": morning,
        "close_forecasts": pl.DataFrame(
            schema={
                "game_date": pl.Date,
                "game_id": pl.Int64,
                "status": pl.String,
                "p_close_hat": pl.Float64,
                "published_utc": qf.UTC_TYPE,
                "forecast_version": pl.String,
            }
        ),
        "odds_snapshots": qf.odds(),
        "dailyfaceoff_goalies": qf.dailyfaceoff(),
    }


def watched() -> dict[str, pl.DataFrame]:
    return {
        "tops": qf.tops().with_columns(pl.lit(qf.DAY).alias("game_date")),
        "books": qf.depth().with_columns(pl.lit(qf.DAY).alias("game_date")),
        "trades": qf.trades([(10, qf.HOME, 0.51, 20.0)]),
    }


def test_before_the_decision_the_slate_is_the_morning_views() -> None:
    # The morning view's row carries the slate's columns, as morning_predictions does.
    morning = qf.ledger(p_blend=0.56, published=qf.PUBLISHED - timedelta(hours=5))
    empty = qf.ledger().clear()
    found = ql.live_inputs(qf.DAY, lake_tables(empty, morning), watched())
    assert [g.game_id for g in found.games] == [qf.GAME]
    rows = ql.live_rows(found, DEMO_EXCHANGE, qf.at(5), "x")
    assert row(rows, "model-1c", qf.HOME)["fair"] == pytest.approx(0.56)
    # Once the decision is published, its slate, and its fair value from its publication.
    decided = ql.live_inputs(qf.DAY, lake_tables(qf.ledger(), morning), watched())
    rows = ql.live_rows(decided, DEMO_EXCHANGE, qf.at(5), "x")
    assert row(rows, "model-1c", qf.HOME)["fair"] == pytest.approx(0.55)
    # Another day's rows are not today's.
    other = ql.live_inputs(date(2026, 11, 4), lake_tables(qf.ledger(), morning), watched())
    assert other.games == ()


def test_the_watch_polls_are_read_once_each_and_an_unfinished_one_later(tmp_path: Path) -> None:
    from kalshi_watch_fixtures import run

    from nhl_edge.ingest import kalshi_watch as kw

    _, _, store, _ = run(tmp_path, 12)
    tables, _ = kw.stored_watch(store)
    day = tables[kw.TOPS]["game_date"][0]
    # The last top poll's meta not written yet: read at a later cycle.
    last = kw.poll_keys(store, kw.TOPS, day)[-1]
    meta = store.base_dir / f"{last}.meta.json"
    hidden = meta.with_suffix(".hidden")
    meta.rename(hidden)
    seen = ql.Watched(store, day)
    first = seen.update()
    assert first[kw.TOPS].height == tables[kw.TOPS].height - 6
    hidden.rename(meta)
    read = len(seen.read)
    second = seen.update()
    assert len(seen.read) == read + 1
    for kind in kw.KINDS:
        assert second[kind].equals(tables[kind])


def test_the_log_line_has_counts_only() -> None:
    rows = ql.live_rows(inputs(), DEMO_EXCHANGE, qf.at(5), "x")
    line = ql.count_line(rows, qf.at(5), 0)
    assert line == "quotes live 18:05Z: 1 games, 6 rows (6 quoting); 0 unparsed polls"
    assert "0.5" not in line and "$" not in line


def test_nhl_real_quotes_live_upserts_one_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from nhl_edge import cli
    from nhl_edge.cli import app
    from nhl_edge.lake import supabase as sb
    from nhl_edge.lake import tables as lt

    sent: list[tuple[str, pl.DataFrame, tuple[str, ...]]] = []

    class Fake:
        def upsert(self, table: str, frame: pl.DataFrame, key: Any) -> int:
            sent.append((table, frame, tuple(key)))
            return frame.height

    class FakeLake:
        def pull(self, table: str) -> None:
            pass

        def read(self, table: str) -> pl.DataFrame:
            return lake_tables(qf.ledger(), qf.ledger().clear())[table]

    class FakeStore:
        def restore_from_r2(self, prefix: str) -> None:
            pass

    class Moment(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return qf.at(5)

    monkeypatch.setattr("nhl_edge.settings.load_env", lambda: None)
    monkeypatch.setattr(sb.Supabase, "from_env", classmethod(lambda cls: Fake()))
    monkeypatch.setattr(lt.Lake, "from_env", classmethod(lambda cls, mirror=False: FakeLake()))
    monkeypatch.setattr(
        "nhl_edge.lake.raw.RawStore.from_env", classmethod(lambda cls, **_: FakeStore())
    )
    monkeypatch.setattr(ql.Watched, "update", lambda self: watched())
    monkeypatch.setattr(cli, "datetime", Moment)
    monkeypatch.setattr("nhl_edge.backtest.reports.version", lambda name, now: "quotes-live-x")
    runner = CliRunner()
    # The rows are of the polls mirrored to R2, and a past moment is never written.
    assert runner.invoke(app, ["real", "quotes-live", "--supabase"]).exit_code == 2
    refused = runner.invoke(
        app, ["real", "quotes-live", "--r2", "--supabase", "--at", "2026-11-03T18:05"]
    )
    assert refused.exit_code == 2 and not sent
    result = runner.invoke(app, ["real", "quotes-live", "--r2", "--supabase", "--once"])
    assert result.exit_code == 0, result.output
    ((table, frame, key),) = sent
    assert (table, key, frame.height) == (ql.TABLE, ql.KEY, 6)
    assert set(frame["code_version"]) == {"quotes-live-x"}
    assert (
        result.output.strip() == "quotes live 18:05Z: 1 games, 6 rows (6 quoting); 0 unparsed polls"
    )


def test_the_rows_sent_are_the_migrations_columns() -> None:
    import re

    migration = Path("supabase/migrations/20261106130000_quote_live.sql").read_text()
    body = re.search(r"create table public\.quote_live \((.*?)\n\);", migration, re.S)
    assert body is not None
    found = [m.group(1) for m in re.finditer(r"^  ([a-z_0-9]+) [a-z]", body.group(1), re.M)]
    # The database sets written_utc.
    assert [c for c in found if c != "primary"] == [*ql.LIVE_SCHEMA, "written_utc"]
    assert f"primary key ({', '.join(ql.KEY)})" in migration
    for state in ql.STATES:
        assert f"'{state}'" in migration
    assert "create policy quote_live_owner_reads on public.quote_live" in migration
