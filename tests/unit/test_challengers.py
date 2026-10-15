"""Challengers (#255, ADR 0038): scored on each complete run bundle of a day after their
registration, written once, and checked by recomputing a stored day from its bundle."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import predict_fixtures as pf
import pytest

from nhl_edge.lake.schemas import ChallengerPredictions
from nhl_edge.live import blend_fit
from nhl_edge.live import bundle as lb
from nhl_edge.live import challengers as lc
from nhl_edge.live import predict as lp

FIT = blend_fit.REPORTS / "blend-live-20261007-89ec631.json"
RECORD = pl.DataFrame({"table": ["slate"]})
NOW = datetime(2026, 10, 8, 9, tzinfo=UTC)
# market_only as if registered the day before the fixture's decision day.
MARKET_ONLY = replace(lc.REGISTRY[0], registered_on=pf.DAY - timedelta(days=1))


class NoLake:
    """A lake no test challenger may read."""

    def read(self, table: str, seasons: Any = None) -> pl.DataFrame:
        raise AssertionError(f"read {table}")


def identity(inputs: lp.Day) -> dict[str, Any]:
    published = inputs.published_utc or inputs.decision_utc
    return {
        "day": inputs.day.isoformat(),
        "decision_utc": inputs.decision_utc.isoformat(),
        "published_utc": published.isoformat(),
        "input_cutoff": lp.input_cutoff(inputs.quotes, inputs.decision_utc).isoformat(),
        "bankroll": inputs.bankroll,
        "blend_version": inputs.live.version,
        "feature_build": inputs.versions["feature_build"],
        "code_version": inputs.versions["code_version"],
        "live_fit": FIT.name,
        "live_fit_sha256": lb.sha256(FIT.read_bytes()),
    }


def decided(store: lb.Store, monkeypatch: pytest.MonkeyPatch, **changes: Any) -> pl.DataFrame:
    """The fixture day, with bets, decided under the committed live fit, its bundle in store and
    its models replayed as decided; returns its ledger."""
    live = blend_fit.load(json.loads(FIT.read_text()))
    versions = {**pf.day().versions, "blend_version": live.version}
    inputs = pf.day(
        live=live, versions=versions, published_utc=pf.DECISION + timedelta(minutes=4), **changes
    )
    ledger = lp.ledger(lp.decide(inputs), inputs)
    rows = lb.inputs(inputs.slate, RECORD, inputs.quotes, None)
    lb.write_once(store, pf.DAY, rows, {}, identity(inputs), {})
    monkeypatch.setattr(lb, "models", lambda _: inputs.fitted)
    return ledger


def computed(store: lb.Store, ledger: pl.DataFrame, challenger: lc.Challenger) -> pl.DataFrame:
    bundle, sha = lc.load(store, pf.DAY)
    return lc.run(
        challenger, bundle, sha, lc.live_fit(bundle), NoLake(), "challengers-x", NOW, ledger
    )


def test_market_only_is_the_ledgers_own_market_only_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    output = computed(store, ledger, MARKET_ONLY)
    ChallengerPredictions.validate(output)
    joined = output.join(ledger, on=["game_date", "game_id"], suffix="_ledger")
    assert joined.height == ledger.height == 3
    # Exactly the ledger's value, game by game: the mechanism adds nothing of its own.
    assert joined["p_home"].equals(joined["p_blend_market"])
    assert joined["status"].to_list() == [lp.PREDICTED] * 3
    manifest = store.get(f"{lb.prefix(pf.DAY)}/{lb.MANIFEST}")
    assert set(output["bundle_sha256"]) == {lb.sha256(manifest)}
    assert set(output["reads"]) == {"{}"}
    assert set(output["challenger"]) == {"market_only"} and set(output["adr"]) == {"0038"}


def test_a_day_on_or_before_the_registration_is_never_due_nor_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    on_the_day = replace(MARKET_ONLY, registered_on=pf.DAY)
    assert lc.due(store, (on_the_day,)) == []
    with pytest.raises(ValueError, match="never its evidence"):
        computed(store, ledger, on_the_day)
    # The real registration is later than the fixture's day: nothing of it is due.
    assert lc.due(store, lc.REGISTRY) == []
    assert lc.due(store, (MARKET_ONLY,)) == [(MARKET_ONLY, pf.DAY)]
    lc.write(store, MARKET_ONLY, pf.DAY, computed(store, ledger, MARKET_ONLY))
    assert lc.due(store, (MARKET_ONLY,)) == []


def test_an_incomplete_bundle_is_not_due(tmp_path: Path) -> None:
    store = lb.LocalStore(tmp_path)
    inputs = pf.day()
    lb.write_files(store, pf.DAY, lb.inputs(inputs.slate, RECORD, inputs.quotes, None), {})
    assert lc.complete_days(store) == []
    assert lc.due(store, (MARKET_ONLY,)) == []


def test_an_output_is_written_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    output = computed(store, ledger, MARKET_ONLY)
    key = lc.write(store, MARKET_ONLY, pf.DAY, output)
    assert key == "challengers/live/market_only/1/2026-10-07.parquet"
    # The same bytes again count as written, as after a write that failed part way.
    lc.write(store, MARKET_ONLY, pf.DAY, output)
    later = output.with_columns(computed_utc=pl.lit(NOW + timedelta(hours=1)))
    with pytest.raises(lb.Conflict):
        lc.write(store, MARKET_ONLY, pf.DAY, later)
    assert lc.sync(store).equals(output)


def test_a_stored_day_is_checked_against_its_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    bundle, _ = lc.load(store, pf.DAY)
    live = lc.live_fit(bundle)
    key = lc.write(store, MARKET_ONLY, pf.DAY, computed(store, ledger, MARKET_ONLY))
    later = NOW + timedelta(days=3)
    assert lc.check(store, MARKET_ONLY, pf.DAY, live, NoLake(), later, ledger) == []
    # Altered bytes are caught: a probability moved, and a status changed.
    stored = pl.read_parquet(tmp_path / key)
    first = stored["game_id"][0]
    moved = stored.with_columns(
        p_home=pl.when(pl.col("game_id") == first).then(pl.col("p_home") + 0.01).otherwise("p_home")
    )
    moved.write_parquet(tmp_path / key)
    assert lc.check(store, MARKET_ONLY, pf.DAY, live, NoLake(), later, ledger) == [
        f"p_home differs for [{first}]"
    ]
    stored.with_columns(status=pl.lit(lp.STALE), p_home=None).write_parquet(tmp_path / key)
    problems = lc.check(store, MARKET_ONLY, pf.DAY, live, NoLake(), later, ledger)
    assert problems[0].startswith("p_home differs") and problems[1].startswith("status differs")


def test_a_bundle_that_does_not_replay_its_ledger_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    moved = ledger.with_columns(stake=pl.col("stake") * 1.01)
    with pytest.raises(ValueError, match="doesn't replay the ledger"):
        computed(store, moved, MARKET_ONLY)


def test_a_game_left_out_or_without_a_probability_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = lb.LocalStore(tmp_path)
    ledger = decided(store, monkeypatch)
    first, second, _ = sorted(ledger["game_id"].to_list())

    def partial(bundle: Any, replayed: pl.DataFrame, live: Any, lake: Any) -> pl.DataFrame:
        return pl.DataFrame(
            {"game_id": [first, second], "p_home": [0.55, None]},
            schema={"game_id": pl.Int64, "p_home": pl.Float64},
        )

    challenger = replace(MARKET_ONLY, name="partial", predict=partial)
    statuses = dict(computed(store, ledger, challenger).select("game_id", "status").iter_rows())
    assert statuses[first] == lc.PREDICTED
    assert statuses[second] == lc.LEFT_OUT
    assert sorted(statuses.values()).count(lc.LEFT_OUT) == 2

    def no_reason(bundle: Any, replayed: pl.DataFrame, live: Any, lake: Any) -> pl.DataFrame:
        return replayed.select("game_id", "status", p_home=pl.lit(None, pl.Float64))

    blank = replace(MARKET_ONLY, name="blank", predict=no_reason)
    assert set(computed(store, ledger, blank)["status"]) == {lc.NO_PROBABILITY}


def test_every_registered_challenger_names_its_adr_and_a_unique_version() -> None:
    decisions = Path("docs/decisions")
    for challenger in lc.REGISTRY:
        assert list(decisions.glob(f"{challenger.adr}-*.md")), challenger.name
    names = [(c.name, c.version) for c in lc.REGISTRY]
    assert len(names) == len(set(names))


def test_nhl_live_challengers_writes_the_due_days_once_and_checks_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app

    out = tmp_path / "dry"
    store = lb.LocalStore(out)
    ledger = decided(store, monkeypatch)
    ledger.write_parquet(out / f"{pf.DAY.isoformat()}.parquet")
    monkeypatch.setattr(lc, "REGISTRY", (MARKET_ONLY,))
    runner = CliRunner()
    result = runner.invoke(app, ["live", "challengers", "--from", str(out)])
    assert result.exit_code == 0, result.output
    assert "market_only 1, 2026-10-07: 3 games, 3 predicted" in result.output
    assert "1 of 1 due outputs written" in result.output
    again = runner.invoke(app, ["live", "challengers", "--from", str(out)])
    assert "0 of 0 due outputs written" in again.output
    check = runner.invoke(app, ["live", "challengers", "--check", "--from", str(out)])
    assert check.exit_code == 0, check.output
    assert "recomputed from its bundle, identical" in check.output
    # A stored output that no longer matches fails the check.
    key = out / MARKET_ONLY.key(pf.DAY)
    pl.read_parquet(key).with_columns(p_home=pl.col("p_home") * 0.99).write_parquet(key)
    check = runner.invoke(app, ["live", "challengers", "--check", "--from", str(out)])
    assert check.exit_code == 1
    assert "p_home differs" in check.output
    # One of --r2 and --from.
    assert runner.invoke(app, ["live", "challengers"]).exit_code != 0


def test_one_broken_challenger_never_stops_the_others_and_check_reads_every_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from nhl_edge.cli import app

    out = tmp_path / "dry"
    store = lb.LocalStore(out)
    ledger = decided(store, monkeypatch)
    ledger.write_parquet(out / f"{pf.DAY.isoformat()}.parquet")

    def broken(bundle: Any, replayed: pl.DataFrame, live: Any, lake: Any) -> pl.DataFrame:
        raise IndexError("a bug in the challenger")

    # Codex on #265: an error of any kind is reported per pair, and the others still run.
    monkeypatch.setattr(
        lc, "REGISTRY", (replace(MARKET_ONLY, name="broken", predict=broken), MARKET_ONLY)
    )
    runner = CliRunner()
    result = runner.invoke(app, ["live", "challengers", "--from", str(out)])
    assert result.exit_code == 1
    assert "broken 1, 2026-10-07: a bug in the challenger" in result.output
    assert "market_only 1, 2026-10-07: 3 games, 3 predicted" in result.output
    assert "1 of 2 due outputs written" in result.output
    # A stored output whose bundle has gone is checked, and fails (Codex on #265).
    (out / lb.prefix(pf.DAY) / lb.MANIFEST).unlink()
    check = runner.invoke(app, ["live", "challengers", "--check", "--from", str(out)])
    assert check.exit_code == 1
    assert "market_only 1, 2026-10-07:" in check.output and "no manifest" in check.output
    assert "0 of 1 stored outputs reproduced" in check.output
